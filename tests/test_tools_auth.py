"""Tools bearer caches preserve the shared login and isolate resource audiences."""

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import _recurse_cli as cli
from tests import test_cli
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService

_ephemeral_callback_port = test_cli._ephemeral_callback_port
keychain = test_cli.keychain
logged_in = test_cli.logged_in
service = test_cli.service


@pytest.fixture
def tools_resource(service: FakeService, monkeypatch: pytest.MonkeyPatch) -> str:
    """Give the HTTP issuer distinct resource bearers without changing agent grants."""
    resource = service.url + "/mcp"
    original = service.oauth
    issued = 0

    def oauth(path: str, body: dict[str, Any] | bytes | None) -> tuple[int, dict[str, Any]]:
        """Issue an identifiable Tools bearer for each requested resource grant."""
        nonlocal issued
        status, response = original(path, body)
        if path == "/v1/oauth/token" and isinstance(body, dict) and "resource" in body:
            issued += 1
            response["access_token"] = f"tools-{issued}"
        return status, response

    monkeypatch.setattr(service, "oauth", oauth)
    return resource


def _tools_cache(url: str) -> tuple[str, str]:
    """Return the separate Tools cache for one issuer origin."""
    return "recurse-cli", f"tools-access-token:{url}"


def test_tools_and_agent_tokens_never_share_audience_cache(
    service: FakeService, logged_in: dict[tuple[str, str], str], tools_resource: str
) -> None:
    """Selecting Tools refreshes the same login into its own resource bearer."""
    assert cli._access_token() == "access-1"
    assert cli._access_token(resource=tools_resource) == "tools-1"
    assert cli._access_token() == "access-1"
    assert cli._access_token(resource=tools_resource) == "tools-1"
    grants = [body for _, path, body, _ in service.requests if path == "/v1/oauth/token"]
    assert grants == [
        {"grant_type": "refresh_token", "client_id": "recurse-cli", "refresh_token": "device-1"},
        {
            "grant_type": "refresh_token",
            "client_id": "recurse-cli",
            "refresh_token": "device-1",
            "resource": tools_resource,
        },
    ]
    assert json.loads(logged_in[_tools_cache(service.url)])["resource"] == tools_resource


@pytest.mark.parametrize("action", ["logout", "replace"])
def test_login_change_clears_both_audience_caches(
    service: FakeService, logged_in: dict[tuple[str, str], str], tools_resource: str, action: str
) -> None:
    """Neither audience bearer survives a completed account replacement or logout."""
    cli._access_token()
    cli._access_token(resource=tools_resource)
    if action == "logout":
        cli._logout()
    else:
        cli._store_device_credential({"refresh_token": "device-2"})
    assert ("recurse-cli", f"access-token:{service.url}") not in logged_in
    assert _tools_cache(service.url) not in logged_in


def test_failed_revoke_preserves_both_audience_caches(
    service: FakeService, logged_in: dict[tuple[str, str], str], tools_resource: str
) -> None:
    """A rejected logout cannot discard either usable token or durable credential."""
    cli._access_token()
    cli._access_token(resource=tools_resource)
    before = dict(logged_in)
    service.fail_detail["/v1/oauth/revoke"] = (503, "unavailable")
    with pytest.raises(cli.ServiceError):
        cli._logout()
    assert logged_in == before


@pytest.mark.parametrize("cache_resource", [None, "http://other.example/mcp", 3])
def test_mismatched_tools_resource_cannot_reuse_a_bearer(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tools_resource: str,
    cache_resource: object,
) -> None:
    """A changed Tools endpoint cannot receive a bearer cached for its predecessor."""
    cli._access_token(resource=tools_resource)
    entry = json.loads(logged_in[_tools_cache(service.url)])
    entry["resource"] = cache_resource
    logged_in[_tools_cache(service.url)] = json.dumps(entry)
    assert cli._access_token(resource=tools_resource) == "tools-2"
    assert len(service.device_grants) == 2


def test_tools_expiry_and_rejected_token_refresh_only_tools(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tools_resource: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The thirty-second lifetime margin and 401 exclusion apply per audience."""
    del logged_in
    started = time.time()
    monkeypatch.setattr(time, "time", lambda: started)
    assert cli._access_token() == "access-1"
    assert cli._access_token(resource=tools_resource) == "tools-1"
    monkeypatch.setattr(time, "time", lambda: started + 869)
    assert cli._access_token(resource=tools_resource) == "tools-1"
    monkeypatch.setattr(time, "time", lambda: started + 870)
    assert cli._access_token(resource=tools_resource) == "tools-2"
    rejected = "tools-2"
    assert cli._access_token(rejected_token=rejected, resource=tools_resource) == "tools-3"
    assert len(service.device_grants) == 4


def test_tools_token_needs_the_current_origin_login(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tools_resource: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither an orphan bearer nor another issuer's login can authorize Tools."""
    cli._access_token(resource=tools_resource)
    del logged_in[("recurse-cli", f"device-credential:{service.url}")]
    with pytest.raises(cli._CliError, match="not logged in"):
        cli._access_token(resource=tools_resource)
    monkeypatch.setenv("RECURSE_API_URL", service.url + "/foreign")
    with pytest.raises(cli._CliError, match="not logged in"):
        cli._access_token(resource=tools_resource)
    assert len(service.device_grants) == 1


def test_concurrent_resource_requests_share_one_refresh(
    service: FakeService, logged_in: dict[tuple[str, str], str], tools_resource: str
) -> None:
    """Twelve consumers use one origin lock rather than racing resource exchanges."""
    del logged_in
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: cli._access_token(resource=tools_resource), range(12)))
    assert results == ["tools-1"] * 12
    rejected = "tools-1"
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(
            workers.map(
                lambda _: cli._access_token(rejected_token=rejected, resource=tools_resource),
                range(12),
            )
        )
    assert results == ["tools-2"] * 12
    assert service.device_grants == ["device-1", "device-1"]


def test_tools_subprocesses_share_resource_cache(service: FakeService, tmp_path: Path) -> None:
    """The OS-backed origin lock also coordinates independent stdio host processes."""
    env = process_environment(tmp_path, service.url)
    command = [
        sys.executable,
        "-c",
        (
            "from _recurse_cli import _access_token; "
            f"print(_access_token(resource={service.url + '/mcp'!r}))"
        ),
    ]
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda _: subprocess.run(  # noqa: S603 - fixed Python test command
                    command, env=env, capture_output=True, check=True
                ),
                range(4),
            )
        )
    assert [item.stdout.strip() for item in results] == [b"access-1"] * 4
    assert service.device_grants == ["device-1"]
