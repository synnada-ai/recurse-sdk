"""Authentication cache safety and concurrency through the public HTTP contract."""

import hashlib
import io
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import keyring.errors
import pytest

import _recurse_cli as cli
from tests import test_cli
from tests.conftest import write_app
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService

_ephemeral_callback_port = test_cli._ephemeral_callback_port
keychain = test_cli.keychain
logged_in = test_cli.logged_in
service = test_cli.service


def cache_key(url: str) -> tuple[str, str]:
    """Return the endpoint's keychain cache key."""
    return "recurse-cli", f"access-token:{url}"


@pytest.mark.parametrize("action", ["logout", "replace"])
def test_login_changes_clear_cached_access(
    service: FakeService, logged_in: dict[tuple[str, str], str], action: str
) -> None:
    """Successful logout and account replacement remove the previous bearer."""
    cli._access_token()
    assert cache_key(service.url) in logged_in
    if action == "logout":
        cli._logout()
        with pytest.raises(cli._CliError, match="not logged in"):
            cli._access_token()
    else:
        cli._store_device_credential({"device_credential": "device-2"})
        assert cli._device_credential() == "device-2"
    assert cache_key(service.url) not in logged_in


def test_failed_logout_preserves_both_credentials(
    service: FakeService, logged_in: dict[tuple[str, str], str]
) -> None:
    """A failed revocation must not destroy the login or usable cached bearer."""
    cli._access_token()
    before = dict(logged_in)
    service.fail_detail["/v1/auth/logout"] = (503, "unavailable")
    with pytest.raises(cli.ServiceError):
        cli._logout()
    assert logged_in == before


def test_parallel_artifacts_reuse_token_and_retry_rejection_once(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired artifact bearer refreshes once without repeating any download."""
    service.token_responses = ["access-old", "access-new"]
    cli._access_token()
    original = service.route
    downloads: list[int] = []

    def route(method: str, path: str, body: Any, token: str | None) -> tuple[int, dict[str, Any]]:
        """Reject the old bearer before returning the real artifact grant."""
        if "/artifacts/" in path and token == "Bearer access-old":  # noqa: S105 - fake bearer
            return 401, {"detail": "expired"}
        return original(method, path, body, token)

    monkeypatch.setattr(service, "route", route)
    original_download = cli._download_artifact

    def download(grant: dict[str, Any]) -> bytes:
        """Count completed real downloads, including their checksum validation."""
        result = original_download(grant)
        downloads.append(1)
        return result

    monkeypatch.setattr(cli, "_download_artifact", download)

    def read(index: int) -> dict[str, Any]:
        """Read the same output as independent host resource requests."""
        return cli._read_mcp_resource(
            {
                "id": index,
                "params": {
                    "uri": f"recurse://artifact/{service.run_id}/99999999-9999-4999-8999-999999999999"
                },
            }
        )[0]

    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(read, range(12)))
    assert [result["id"] for result in results] == list(range(12))
    assert all(result["result"]["contents"][0]["blob"] for result in results)
    assert len(downloads) == 12
    assert service.device_grants == ["device-1", "device-1"]


@pytest.mark.parametrize(
    "rejected_path",
    [
        "/v1/agent-versions",
        "/v1/agent-versions/version-1/complete",
        "/v1/agent-versions/version-1",
    ],
)
def test_preparation_refreshes_one_rejected_bearer(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejected_path: str,
) -> None:
    """Preparation survives one API 401 without repeating its source upload."""
    del logged_in
    service.token_responses = ["access-old", "access-new"]
    app = write_app(tmp_path / "app")
    original = service.route
    rejected = False

    def route(method: str, path: str, body: Any, token: str | None) -> tuple[int, dict[str, Any]]:
        """Reject the old bearer exactly once at the selected preparation step."""
        nonlocal rejected
        if path == rejected_path and token == "Bearer access-old" and not rejected:  # noqa: S105
            rejected = True
            return 401, {"detail": "expired"}
        if token == "Bearer access-old":  # noqa: S105 - still-valid fixture bearer
            service.token_responses.insert(0, "access-old")
            try:
                return original(method, path, body, token)
            finally:
                service.token_responses.pop(0)
        return original(method, path, body, token)

    monkeypatch.setattr(service, "route", route)
    monkeypatch.setattr(cli, "_POLL_SECONDS", 0)

    assert cli.main(["deploy", str(app), "--as", "mcp"]) == 0
    assert rejected is True
    assert service.device_grants == ["device-1", "device-1"]
    assert len([request for request in service.requests if request[1] == "/direct-upload"]) == 1
    attempted_tokens = [
        token for _method, path, _body, token in service.requests if path == rejected_path
    ]
    assert attempted_tokens[:2] == ["Bearer access-old", "Bearer access-new"]


@pytest.mark.parametrize("case", [(401, 2, 2), (400, 1, 1)])
def test_preparation_authentication_retry_is_bounded(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    case: tuple[int, int, int],
) -> None:
    """Only one definite 401 refreshes; another failure remains terminal."""
    del logged_in
    status, expected_requests, expected_grants = case
    service.token_responses = ["access-old", "access-new"]
    service.fail_detail["/v1/agent-versions"] = (status, "denied")

    assert cli.main(["deploy", str(write_app(tmp_path / "app")), "--as", "mcp"]) == 1
    attempts = [request for request in service.requests if request[1] == "/v1/agent-versions"]
    assert len(attempts) == expected_requests
    assert len(service.device_grants) == expected_grants


@pytest.mark.parametrize("rejected_request", ["status", "grant"])
def test_cli_artifacts_refreshes_one_rejected_bearer(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejected_request: str,
) -> None:
    """Artifact retrieval refreshes one API 401 and downloads the bytes once."""
    del logged_in
    service.token_responses = ["access-old", "access-new"]
    service.run_views = [service.run_views[-1]]
    status_path = f"/v1/runs/{service.run_id}"
    grant_path = f"{status_path}/artifacts/99999999-9999-4999-8999-999999999999"
    rejected_path = status_path if rejected_request == "status" else grant_path
    original_route = service.route
    rejected = False

    def route(method: str, path: str, body: Any, token: str | None) -> tuple[int, dict[str, Any]]:
        """Reject the old bearer exactly once before returning the real response."""
        nonlocal rejected
        if path == rejected_path and token == "Bearer access-old" and not rejected:  # noqa: S105
            rejected = True
            return 401, {"detail": "expired"}
        if token == "Bearer access-old":  # noqa: S105 - still-valid fixture bearer
            service.token_responses.insert(0, "access-old")
            try:
                return original_route(method, path, body, token)
            finally:
                service.token_responses.pop(0)
        return original_route(method, path, body, token)

    monkeypatch.setattr(service, "route", route)
    original_download = cli._download_artifact
    downloads = 0

    def download(grant: dict[str, Any]) -> bytes:
        """Count the real verified download without replacing it."""
        nonlocal downloads
        downloads += 1
        return original_download(grant)

    monkeypatch.setattr(cli, "_download_artifact", download)

    assert cli.main(["artifacts", service.run_id, "--output", str(tmp_path)]) == 0
    assert rejected is True
    assert service.device_grants == ["device-1", "device-1"]
    assert downloads == 1
    attempted_tokens = [
        token for _method, path, _body, token in service.requests if path == rejected_path
    ]
    assert attempted_tokens == ["Bearer access-old", "Bearer access-new"]


def test_cli_artifacts_preserves_status_gateway_retry(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
) -> None:
    """Authentication recovery must retain the existing safe status retry."""
    del logged_in
    status_path = f"/v1/runs/{service.run_id}"
    service.transient_failures[status_path] = 1

    assert cli.main(["artifacts", service.run_id, "--output", str(tmp_path)]) == 0
    assert len([request for request in service.requests if request[1] == status_path]) == 2
    assert service.device_grants == ["device-1"]


@pytest.mark.parametrize("status", [401, 429, 503])
def test_artifact_failure_is_bounded(
    service: FakeService, logged_in: dict[tuple[str, str], str], status: int
) -> None:
    """Only a definite 401 may repeat the grant request, and only once."""
    path = f"/v1/runs/{service.run_id}/artifacts/99999999-9999-4999-8999-999999999999"
    service.fail_detail[path] = (status, "denied")
    with pytest.raises(cli.ServiceError) as failure:
        cli._read_mcp_resource(
            {
                "id": 1,
                "params": {
                    "uri": f"recurse://artifact/{service.run_id}/99999999-9999-4999-8999-999999999999"
                },
            }
        )
    assert failure.value.status_code == status
    expected = 2 if status == 401 else 1
    assert len([r for r in service.requests if r[1] == path]) == expected
    assert len(service.device_grants) == expected


def test_expiry_refresh_uses_server_lifetime(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tokens refresh thirty seconds before expiry without extending their lifetime."""
    service.token_responses = ["access-old", "access-new"]
    started = time.time()
    monkeypatch.setattr(time, "time", lambda: started)
    assert cli._access_token() == "access-old"
    monkeypatch.setattr(time, "time", lambda: started + 869)
    assert cli._access_token() == "access-old"
    monkeypatch.setattr(time, "time", lambda: started + 870)
    assert cli._access_token() == "access-new"
    assert service.device_grants == ["device-1", "device-1"]


@pytest.mark.parametrize("value", [None, True, "900", 0, -1, float("nan"), float("inf")])
def test_invalid_lifetime_is_not_cached(
    service: FakeService, logged_in: dict[tuple[str, str], str], value: Any
) -> None:
    """An unbounded or untrusted lifetime must never produce a reusable bearer."""
    service.malformed["/v1/auth/token"] = {"access_token": "test-token", "expires_in": value}
    with pytest.raises(cli.ServiceError, match="expires_in"):
        cli._access_token()
    assert cache_key(service.url) not in logged_in


@pytest.mark.parametrize("raw", ["{", "null", "[]", "{}"])
def test_malformed_cache_is_replaced(
    service: FakeService, logged_in: dict[tuple[str, str], str], raw: str
) -> None:
    """Damaged cache data does not lose the durable login or escape as a JSON error."""
    logged_in[cache_key(service.url)] = raw
    assert cli._access_token() == "access-1"
    assert cli._device_credential() == "device-1"
    assert service.device_grants == ["device-1"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_token", None),
        ("access_token", ""),
        ("expires_at", None),
        ("expires_at", True),
        ("expires_at", float("inf")),
        ("expires_at", 0),
        ("credential_sha256", "wrong-login"),
    ],
)
def test_invalid_cache_fields_cannot_be_reused(
    service: FakeService, logged_in: dict[tuple[str, str], str], field: str, value: Any
) -> None:
    """Cache field validation protects expiry and account boundaries."""
    entry = {
        "access_token": "wrong-token",
        "expires_at": time.time() + 900,
        "credential_sha256": hashlib.sha256(b"device-1").hexdigest(),
    }
    entry[field] = value
    logged_in[cache_key(service.url)] = json.dumps(entry)
    assert cli._access_token() == "access-1"
    assert len(service.device_grants) == 1


def test_missing_login_never_uses_cache(
    service: FakeService, logged_in: dict[tuple[str, str], str]
) -> None:
    """Even a valid cached bearer cannot replace a removed device login."""
    cli._access_token()
    del logged_in[("recurse-cli", f"device-credential:{service.url}")]
    with pytest.raises(cli._CliError, match="not logged in"):
        cli._access_token()
    assert len(service.device_grants) == 1


def test_remote_revocation_is_observed_at_refresh(
    service: FakeService, logged_in: dict[tuple[str, str], str]
) -> None:
    """Remote PAT revocation does not revoke an already-issued bearer immediately."""
    assert cli._access_token() == "access-1"
    service.fail_detail["/v1/auth/token"] = (401, "credential revoked")
    assert cli._access_token() == "access-1"
    with pytest.raises(cli.ServiceError, match="revoked"):
        cli._access_token(rejected_token="access-1")  # noqa: S106 - fake bearer
    assert cache_key(service.url) not in logged_in


def test_api_origins_and_logins_cannot_share_tokens(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching origin or saved credential forces an independent exchange."""
    assert cli._access_token() == "access-1"
    logged_in[("recurse-cli", f"device-credential:{service.url}")] = "device-2"
    cli._access_token()
    assert service.device_grants == ["device-1", "device-2"]
    other = FakeService()
    try:
        monkeypatch.setenv("RECURSE_API_URL", other.url)
        cli._store_device_credential({"device_credential": "device-2"})
        cli._access_token()
        assert other.device_grants == ["device-2"]
    finally:
        other.close()


@pytest.mark.parametrize("operation", ["get_password", "delete_password"])
def test_cache_keyring_failure_is_reported_safely(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Cache access errors are reported without printing credentials or returning stale tokens."""
    cli._access_token()
    before = dict(logged_in)

    def fail(*args: object) -> None:
        """Simulate the keyring becoming unavailable."""
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr(keyring, operation, fail)
    with pytest.raises(cli._CliError, match="keychain"):
        cli._access_token(rejected_token="access-1")  # noqa: S106 - fake bearer
    assert service.device_grants == ["device-1"]
    assert logged_in == before


@pytest.mark.parametrize("cached", [False, True])
def test_cache_write_failure_keeps_fresh_token_without_hiding_degraded_reuse(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cached: bool,
) -> None:
    """A cache-only write failure must not discard a valid exchange or reuse a rejected token."""
    service.token_responses = ["access-old", "access-new", "access-next"]
    if cached:
        assert cli._access_token() == "access-old"

    def fail(*args: object) -> None:
        """Reject only keyring writes, with sensitive text that must not reach stderr."""
        raise keyring.errors.PasswordSetError("device-1 access-new")

    monkeypatch.setattr(keyring, "set_password", fail)
    token = cli._access_token(rejected_token="access-old" if cached else None)
    assert token == ("access-new" if cached else "access-old")
    assert cache_key(service.url) not in logged_in
    assert cli._device_credential() == "device-1"
    assert service.device_grants == ["device-1"] * (2 if cached else 1)
    output = capsys.readouterr()
    assert output.out == ""
    assert "cannot save" in output.err
    assert "reuse" in output.err
    assert "device-1" not in output.err
    assert "access-" not in output.err

    # A failed cache write must not leave an in-memory bearer reused on the next call.
    assert cli._access_token() == ("access-next" if cached else "access-new")
    assert service.device_grants == ["device-1"] * (3 if cached else 2)


@pytest.mark.parametrize("arguments,exit_code", [(["--help"], 0), (["billing", "balance"], 1)])
def test_missing_home_only_blocks_commands_requiring_authentication(
    service: FakeService, tmp_path: Path, arguments: list[str], exit_code: int
) -> None:
    """Missing home resolution cannot break help; auth fails clearly before any exchange."""
    env = process_environment(tmp_path, service.url)
    result = subprocess.run(  # noqa: S603 - fixed interpreter and isolated synthetic environment
        [
            sys.executable,
            "-c",
            "import os.path, sys; os.path.expanduser = lambda path: path; "
            "from _recurse_cli import main; raise SystemExit(main(sys.argv[1:]))",
            *arguments,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == exit_code
    if exit_code == 0:
        assert "usage: recurse" in result.stdout
        assert result.stderr == ""
    else:
        assert result.stdout == ""
        assert "login lock" in result.stderr
        assert "Traceback" not in result.stderr
    assert service.requests == []


def test_lock_timeout_and_permissions(
    service: FakeService, logged_in: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contenders time out; the lock contains no secrets and has owner-only permissions."""
    monkeypatch.setattr(cli, "_REQUEST_TIMEOUT_SECONDS", 0)
    errors: list[str] = []

    def contend() -> None:
        """Try acquiring the same endpoint's lock from another thread."""
        try:
            cli._access_token()
        except cli._CliError as error:
            errors.append(str(error))

    with cli._auth_lock():
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert errors == ["another Recurse process is refreshing your login; try again"]
    lock = next(cli._AUTH_LOCK_DIRECTORY.glob("*.lock"))
    assert lock.read_bytes() == b""
    assert lock.stat().st_mode & 0o777 == 0o600
    assert service.device_grants == []


@pytest.mark.parametrize("missing_home", [False, True])
def test_unavailable_lock_directory_does_not_exchange(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing_home: bool,
) -> None:
    """A filesystem failure cannot bypass refresh coordination."""
    path = tmp_path / "not-a-directory"
    path.write_text("occupied")
    monkeypatch.setattr(cli, "_AUTH_LOCK_DIRECTORY", path)
    if missing_home:

        def fail_expanduser(self: Path) -> Path:
            """Simulate Python failing to resolve a home directory."""
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(Path, "expanduser", fail_expanduser)
    with pytest.raises(cli._CliError, match="login lock"):
        cli._access_token()
    assert service.device_grants == []


def test_process_exit_releases_auth_lock(service: FakeService, tmp_path: Path) -> None:
    """A killed refresh owner cannot leave subsequent CLI processes deadlocked."""
    env = process_environment(tmp_path, service.url)
    command = [
        sys.executable,
        "-c",
        (
            "import time; from _recurse_cli import _auth_lock\n"
            "with _auth_lock():\n"
            " print('locked', flush=True)\n"
            " time.sleep(30)\n"
        ),
    ]
    owner = subprocess.Popen(  # noqa: S603 - fixed synthetic local test
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "locked"
    finally:
        owner.kill()
        owner.communicate(timeout=5)
    result = subprocess.run(
        [sys.executable, "-c", "from _recurse_cli import _access_token; print(_access_token())"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert result.stdout.strip() == "access-1"
    assert result.stderr == ""
    assert service.device_grants == ["device-1"]


@pytest.mark.parametrize("rejection", [None, "tools/call", "tasks/get"])
def test_concurrent_bridge_calls_deliver_every_admitted_result(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    rejection: str | None,
) -> None:
    """Twelve real host calls survive concurrent rejection without duplicate submissions."""
    service.token_responses = ["access-old", "access-new"] if rejection else ["access-old"]
    admitted: list[int] = []
    rejected: list[int] = []
    barrier = threading.Barrier(12)

    def handler(message: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any]]:
        """Emulate admission and completed task polling behind a rejecting bearer."""
        request_id, method = message["id"], message["method"]
        if method == rejection and token == "Bearer access-old":  # noqa: S105 - test token
            rejected.append(request_id)
            barrier.wait(timeout=10)
            return 401, {"detail": "expired"}
        if method == "server/discover":
            return 200, test_cli._remote_discovery_reply(request_id)
        if method == "tools/call":
            admitted.append(request_id)
            result = {"taskId": str(request_id), "status": "working", "pollIntervalMs": 1}
        else:
            assert method == "tasks/get"
            result = {
                "taskId": str(request_id),
                "status": "completed",
                "result": {"content": [{"type": "text", "text": str(request_id)}]},
            }
        return 200, test_cli._remote_reply(request_id, result)

    service.mcp_handler = handler
    inputs = test_cli._StreamingInput()
    outputs = io.BytesIO()
    errors: list[BaseException] = []

    def serve() -> None:
        """Keep the bridge alive until every admitted task's result arrives."""
        try:
            cli._serve_mcp("mcp_test", inputs, outputs)
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        inputs.send(test_cli._initialize_frame("init"))
        responses: list[dict[str, Any] | None] = []
        for wave in range(2):
            indices = range(wave * 12, (wave + 1) * 12)
            for index in indices:
                inputs.send(
                    {
                        "jsonrpc": "2.0",
                        "id": index,
                        "method": "tools/call",
                        "params": {"name": "tune", "arguments": {}},
                    }
                )
            responses.extend(
                test_cli._wait_for_host_response(outputs, index, timeout=10) for index in indices
            )
    finally:
        inputs.close()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert errors == []
    assert sorted(admitted) == list(range(24))
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": index,
            "result": {
                "content": [
                    {"type": "text", "text": str(index)},
                    {"type": "text", "text": f"Task: {index}"},
                ]
            },
        }
        for index in range(24)
    ]
    assert service.device_grants == ["device-1"] * (2 if rejection else 1)
    assert len(rejected) == (12 if rejection else 0)


def test_direct_run_rejection_refreshes_cached_token(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct-run one-401 retry must not reuse the bearer it just rejected."""
    service.token_responses = ["access-old", "access-new"]
    old = cli._access_token()
    original = service.route

    def route(method: str, path: str, body: Any, token: str | None) -> tuple[int, dict[str, Any]]:
        """Reject the expired bearer before reading the run, over real HTTP."""
        if token == "Bearer access-old":  # noqa: S105 - fake bearer
            return 401, {"detail": "expired"}
        return original(method, path, body, token)

    monkeypatch.setattr(service, "route", route)
    result, current = cli._run_request("GET", f"/v1/runs/{service.run_id}", token=old)
    assert current == "access-new"
    assert result["run_id"] == service.run_id
    assert service.device_grants == ["device-1", "device-1"]
