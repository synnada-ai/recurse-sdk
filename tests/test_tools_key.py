"""Customer Tools keys enter through stdin and stay separate from agent logins."""

import io
import json
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import keyring.errors
import pytest

import _recurse_cli as cli
from recurse import ToolsClient, ToolsError
from tests import test_cli
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService
from tests.test_tools_client import ToolsService

_ephemeral_callback_port = test_cli._ephemeral_callback_port
keychain = test_cli.keychain
service = test_cli.service
KEY = "rck_" + "a" * 43


@pytest.fixture
def key_service(service: FakeService, monkeypatch: pytest.MonkeyPatch) -> FakeService:
    """Implement the public key exchange contract over the existing real HTTP double."""
    original = service.oauth
    grants = 0

    def oauth(path: str, body: dict[str, Any] | bytes | None) -> tuple[int, dict[str, Any]]:
        """Keep ordinary CLI grants intact while issuing resource-bound Tools bearers."""
        nonlocal grants
        if isinstance(body, dict) and body.get("client_id") == "recurse-tools-key":
            if path == "/v1/oauth/revoke":
                return 200, {}
            if body.get("refresh_token") != KEY:
                return 400, {"error": "invalid_grant", "error_description": KEY}
            if body.get("resource") != service.url + "/mcp":
                return 400, {"error": "invalid_target"}
            grants += 1
            return 200, {
                "access_token": f"tools-key-{grants}",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        return original(path, body)

    monkeypatch.setattr(service, "oauth", oauth)
    monkeypatch.setenv("RECURSE_TOOLS_URL", service.url)
    return service


def test_key_stdin_bootstraps_tools_without_browser_or_agent_login(
    key_service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Import verifies the key and gives from_login a cached Tools bearer only."""
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO((KEY + "\n").encode())))
    assert cli.main(["tools", "login", "--key-stdin"]) == 0
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert "keychain" in output.out
    assert ToolsClient.from_login()._access_token == "tools-key-1"  # noqa: S105 - synthetic bearer
    with pytest.raises(cli._CliError, match="not logged in"):
        cli._access_token()
    requests = [row[2] for row in key_service.requests if row[1] == "/v1/oauth/token"]
    assert requests == [
        {
            "grant_type": "refresh_token",
            "client_id": "recurse-tools-key",
            "refresh_token": KEY,
            "resource": key_service.url + "/mcp",
        }
    ]
    assert KEY in keychain.values()


@pytest.fixture
def imported_key(
    key_service: FakeService, keychain: dict[tuple[str, str], str]
) -> dict[tuple[str, str], str]:
    """Represent a key imported by the separate stdin command."""
    keychain["recurse-cli", f"tools-key:{key_service.url}:{key_service.url}/mcp"] = KEY
    return keychain


def test_key_and_agent_logins_are_independent(
    key_service: FakeService, imported_key: dict[tuple[str, str], str]
) -> None:
    """A Tools key takes priority without replacing the legacy agent account."""
    cli._store_device_credential({"refresh_token": "device-1"})
    assert cli._access_token() == "access-1"
    assert ToolsClient.from_login()._access_token == "tools-key-1"  # noqa: S105 - synthetic bearer
    cli._logout()
    assert ToolsClient.from_login()._access_token == "tools-key-1"  # noqa: S105 - synthetic bearer
    assert KEY in imported_key.values()
    cli._store_device_credential({"refresh_token": "device-1"})
    assert cli.main(["tools", "logout"]) == 0
    assert KEY not in imported_key.values()
    assert cli._device_credential() == "device-1"
    assert ToolsClient.from_login()._access_token == "access-1"  # noqa: S105 - synthetic bearer
    revoke = [r[2] for r in key_service.requests if r[1] == "/v1/oauth/revoke"]
    assert revoke[-1] == {"client_id": "recurse-tools-key", "token": KEY}


@pytest.mark.parametrize(
    "value", [b"", b"rck_short", b"\xff", b"x" * 129, (KEY + " extra").encode()]
)
def test_key_import_rejects_invalid_stdin_without_echo(
    key_service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: bytes,
) -> None:
    """Invalid pasted material is neither sent to the issuer nor printed."""
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(value)))
    assert cli.main(["tools", "login", "--key-stdin"]) == 2
    assert keychain == {} and key_service.requests == []
    output = capsys.readouterr()
    assert KEY not in output.out + output.err


@pytest.mark.parametrize("failure", ["tty", "read"])
def test_key_import_requires_non_echoing_readable_stdin(
    key_service: FakeService,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """An interactive echoed terminal and a failed pipe cannot import a credential."""
    source = io.TextIOWrapper(io.BytesIO(KEY.encode()))
    if failure == "tty":
        monkeypatch.setattr(source, "isatty", lambda: True)
    else:
        source.close()
    monkeypatch.setattr("sys.stdin", source)
    assert cli.main(["tools", "login", "--key-stdin"]) == 2
    assert key_service.requests == []


def test_rejected_key_preserves_previous_login_and_hides_remote_details(
    key_service: FakeService,
    imported_key: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An invalid replacement cannot destroy an existing key or expose response echoes."""
    previous = dict(imported_key)
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(("rck_" + "b" * 43).encode())))
    assert cli.main(["tools", "login", "--key-stdin"]) == 2
    assert imported_key == previous
    output = capsys.readouterr()
    assert KEY not in output.out + output.err


def test_rejected_key_does_not_fall_back_to_broader_agent_login(
    key_service: FakeService, imported_key: dict[tuple[str, str], str]
) -> None:
    """Revocation cannot be bypassed by silently borrowing another saved authority."""
    imported_key["recurse-cli", f"tools-key:{key_service.url}:{key_service.url}/mcp"] = (
        "revoked-key"
    )
    cli._store_device_credential({"refresh_token": "device-1"})
    with pytest.raises(ToolsError, match="could not be refreshed") as caught:
        ToolsClient.from_login()
    assert KEY not in "".join(traceback.format_exception(caught.value))
    assert key_service.device_grants == []


def test_key_bearer_expiry_rejection_and_parallel_refresh(
    key_service: FakeService,
    imported_key: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One shared auth lock prevents duplicate refreshes across threads."""
    del imported_key
    started = time.time()
    monkeypatch.setattr(time, "time", lambda: started)

    def read(rejected: str | None = None) -> str:
        """Request the exact Tools resource token through the normal client path."""
        return cli._access_token(rejected_token=rejected, resource=key_service.url + "/mcp")

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert list(workers.map(lambda _: read(), range(8))) == ["tools-key-1"] * 8
    monkeypatch.setattr(time, "time", lambda: started + 3570)
    assert read() == "tools-key-2"
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert list(workers.map(lambda _: read("tools-key-2"), range(8))) == ["tools-key-3"] * 8


def test_key_caches_are_bound_to_credential_resource_and_issuer(
    key_service: FakeService,
    imported_key: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another origin, resource or imported credential cannot reuse the cached bearer."""
    ToolsClient.from_login()
    with pytest.raises(ToolsError):
        ToolsClient.from_login(base_url="https://different.example")
    assert KEY in imported_key.values()
    assert ToolsClient.from_login()._access_token == "tools-key-1"  # noqa: S105 - synthetic bearer
    imported_key["recurse-cli", f"tools-key:{key_service.url}:{key_service.url}/mcp"] = (
        "revoked-key"
    )
    with pytest.raises(ToolsError):
        ToolsClient.from_login()
    monkeypatch.setenv("RECURSE_API_URL", "http://different.example")
    with pytest.raises(ToolsError, match="not logged in"):
        ToolsClient.from_login()


def test_failed_key_logout_preserves_key_and_cached_bearer(
    key_service: FakeService, imported_key: dict[tuple[str, str], str]
) -> None:
    """A rejected revocation does not discard the customer's usable local credential."""
    ToolsClient.from_login()
    before = dict(imported_key)
    key_service.fail_detail["/v1/oauth/revoke"] = (503, KEY)
    assert cli.main(["tools", "logout"]) == 2
    assert imported_key == before


def test_key_logout_requires_an_imported_key(
    key_service: FakeService, keychain: dict[tuple[str, str], str]
) -> None:
    """The separate command never revokes an agent login by accident."""
    del key_service, keychain
    assert cli.main(["tools", "logout"]) == 2


def test_key_logout_without_cached_bearer(
    key_service: FakeService, imported_key: dict[tuple[str, str], str]
) -> None:
    """An imported key can be revoked before any bearer is cached."""
    del key_service
    assert cli.main(["tools", "logout"]) == 0
    assert not imported_key


@pytest.mark.parametrize("operation", ["get_password", "set_password", "delete_password"])
def test_keychain_failures_do_not_echo_customer_key(
    key_service: FakeService,
    imported_key: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operation: str,
) -> None:
    """Backend diagnostic text cannot expose a key on the public command boundary."""
    del key_service, imported_key

    def fail(*args: object) -> None:
        """Make a synthetic backend error that includes the credential."""
        raise keyring.errors.KeyringError(KEY)

    monkeypatch.setattr("keyring." + operation, fail)
    if operation == "set_password":
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(KEY.encode())))
        command = ["tools", "login", "--key-stdin"]
    else:
        command = ["tools", "logout"]
    assert cli.main(command) == 2
    output = capsys.readouterr()
    assert "keychain" in output.err and KEY not in output.out + output.err


def test_key_subprocesses_share_resource_cache(key_service: FakeService, tmp_path: Path) -> None:
    """Independent MCP processes coordinate through the real process lock and keychain API."""
    env = process_environment(tmp_path, key_service.url)
    env["RECURSE_TOOLS_URL"] = key_service.url
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            "from _recurse_cli import main; raise SystemExit(main())",
            "tools",
            "login",
            "--key-stdin",
        ],
        input=KEY.encode(),
        env=env,
        capture_output=True,
        check=True,
    )
    assert KEY.encode() not in imported.stdout + imported.stderr
    command = [sys.executable, "-c", "from recurse import ToolsClient; ToolsClient.from_login()"]
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda _: subprocess.run(  # noqa: S603 - fixed test program
                    command,
                    env=env,
                    capture_output=True,
                    check=True,
                ),
                range(4),
            )
        )
    assert all(not r.stdout and not r.stderr for r in results)
    grants = [r for r in key_service.requests if r[1] == "/v1/oauth/token"]
    assert len(grants) == 1


def test_key_cache_write_warning_does_not_echo_credentials(
    key_service: FakeService,
    imported_key: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed optional cache write does not discard a freshly exchanged token."""
    del key_service, imported_key

    def fail(*args: object) -> None:
        """Simulate an unavailable keychain write with a deliberately sensitive error."""
        raise keyring.errors.KeyringError(KEY)

    monkeypatch.setattr("keyring.set_password", fail)
    assert ToolsClient.from_login()._access_token == "tools-key-1"  # noqa: S105 - synthetic bearer
    output = capsys.readouterr()
    assert "warning" in output.err and KEY not in output.err


@pytest.mark.parametrize("token_type", [None, "Basic"])
def test_key_import_rejects_wrong_token_type(
    key_service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    token_type: str | None,
) -> None:
    """Only the agreed bearer response may become a saved customer login."""
    key_service.malformed["/v1/oauth/token"] = {
        "access_token": "tools-key-1",
        "expires_in": 3600,
        "token_type": token_type,
    }
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(KEY.encode())))
    assert cli.main(["tools", "login", "--key-stdin"]) == 2
    assert not keychain


def test_key_request_refuses_redirects_and_credential_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplier redirect or credentials in an issuer URL cannot receive the key."""
    target = ToolsService()
    try:
        target.responses["POST", "/v1/oauth/token"] = (302, b"", {"Location": target.url + "/leak"})
        monkeypatch.setenv("RECURSE_API_URL", target.url)
        with pytest.raises(cli.ServiceError, match="key request failed"):
            cli._tools_key_request("/v1/oauth/token", KEY, resource=target.url + "/mcp")
        assert len(target.requests) == 1 and target.requests[0][1] == "/v1/oauth/token"
        monkeypatch.setenv("RECURSE_API_URL", "https://user:password@example.test")
        with pytest.raises(ToolsError, match="origin"):
            cli._tools_key_request("/v1/oauth/token", KEY, resource=target.url + "/mcp")
    finally:
        target.server.shutdown()
        target.server.server_close()
        target.thread.join()


def test_key_replacement_preserves_agent_cache_and_replaces_tools_identity(
    key_service: FakeService,
    imported_key: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reimporting a verified key refreshes only its own partition."""
    cli._store_device_credential({"refresh_token": "device-1"})
    cli._access_token()
    saved_agent = imported_key["recurse-cli", f"access-token:{key_service.url}"]
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(KEY.encode())))
    assert cli.main(["tools", "login", "--key-stdin"]) == 0
    assert imported_key["recurse-cli", f"access-token:{key_service.url}"] == saved_agent
    name = f"tools-key-access-token:{key_service.url}:{key_service.url}/mcp"
    cache = json.loads(imported_key["recurse-cli", name])
    assert cache["resource"] == key_service.url + "/mcp"
    assert KEY not in imported_key["recurse-cli", name]
