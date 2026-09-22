"""Tests for the `recurse` command-line interface.

The CLI is exercised against a local HTTP double that implements the public
Recurse service contract, so every network interaction crosses a real socket
boundary while remaining deterministic and offline.
"""

import base64
import hashlib
import http.client
import io
import json
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import keyring.errors
import pytest

import _recurse_cli as cli
from _recurse_cli import _LoginServer, main
from tests.conftest import write_app
from tests.keyring_backend import process_environment


class FakeService:
    """An in-process double of the public Recurse service."""

    def __init__(self) -> None:
        """Start the double on an ephemeral loopback port."""
        self.requests: list[tuple[str, str, dict[str, Any] | bytes | None, str | None]] = []
        self.version_statuses: list[str] = ["processing", "ready"]
        self.version_error = "build_failed"
        self.version_model: str | None = None
        self.fail_detail: dict[str, tuple[int, str]] = {}
        self.transient_failures: dict[str, int] = {}
        self.malformed: dict[str, dict[str, Any]] = {}
        self.device_grants: list[str] = []
        self.token_responses: list[str] = ["access-1"]
        self.mcp_bodies: list[bytes] = []
        self.mcp_headers: list[dict[str, str]] = []
        self.mcp_failures: list[tuple[int, dict[str, Any]]] = []
        self.mcp_poll_failures: list[tuple[int, dict[str, Any]]] = []
        self.mcp_raw_responses: list[bytes] = []
        self.mcp_handler: (
            Callable[[dict[str, Any], str | None], tuple[int, dict[str, Any]]] | None
        ) = None
        self.mcp_responses: list[dict[str, Any]] = []
        self.mcp_live_task_id: str | None = None
        self.artifact_bytes = b'{"f1":0.8}'
        self.artifact_sha256 = hashlib.sha256(self.artifact_bytes).hexdigest()
        self.artifact_download_authorization: str | None = None
        self.artifact_download_user_agent: str | None = None
        self.direct_upload_content_type: str | None = None
        self.direct_upload_user_agent: str | None = None
        self.direct_upload_failures = 0
        self.direct_upload_response_losses = 0
        self.direct_upload_stored: set[str] = set()
        self.run_id = "77777777-7777-4777-8777-777777777777"
        self.wallet_balance_microusd = 5_000_000
        self.wallet_available_microusd = 4_921_276
        self.runtime_secret_id = "22222222-2222-4222-8222-222222222222"  # noqa: S105 - fixture ID
        self.runtime_secrets: list[dict[str, Any]] = []
        self.run_views: list[dict[str, Any]] = [
            {
                "run_id": self.run_id,
                "status": "queued",
                "result": None,
                "error": None,
                "artifacts": [],
                "payload_expired": False,
            },
            {
                "run_id": self.run_id,
                "status": "succeeded",
                "result": {"answer": "done"},
                "error": None,
                "artifacts": [
                    {
                        "output_id": "99999999-9999-4999-8999-999999999999",
                        "path": "results/receipt.json",
                        "size_bytes": len(self.artifact_bytes),
                        "sha256": self.artifact_sha256,
                    }
                ],
                "payload_expired": False,
            },
        ]
        service = self

        class Handler(BaseHTTPRequestHandler):
            """Record every request and answer from the scripted routes."""

            def log_message(self, *args: object) -> None:
                """Keep the test output free of request logging."""
                del args

            def _reply(self, status: int, payload: dict[str, Any]) -> None:
                """Send one JSON response."""
                body = b"" if status == 204 else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _reply_raw(self, status: int, body: bytes) -> None:
                """Send one exact JSON response body."""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:  # noqa: PLR0911,PLR0912,PLR0915 - explicit contract double
                """Record the request and answer it from the scripted routes."""
                path = self.path
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                content_type = self.headers.get("Content-Type")
                if path.startswith("/mcp/"):
                    request_payload = json.loads(raw)
                    service.mcp_bodies.append(raw)
                    service.mcp_headers.append(dict(self.headers.items()))
                    service.requests.append(
                        (self.command, path, raw, self.headers.get("Authorization"))
                    )
                    method = self.headers.get("Mcp-Method")
                    if service.mcp_handler is not None:
                        status, payload = service.mcp_handler(
                            request_payload, self.headers.get("Authorization")
                        )
                        self._reply(status, payload)
                        return
                    if method == "tasks/get" and service.mcp_poll_failures:
                        status, payload = service.mcp_poll_failures.pop(0)
                        self._reply(status, payload)
                    elif method == "tasks/get" and service.mcp_live_task_id is not None:
                        self._reply(
                            200,
                            _remote_reply(
                                request_payload.get("id"),
                                {
                                    "resultType": "complete",
                                    "taskId": service.mcp_live_task_id,
                                    "status": "working",
                                    "pollIntervalMs": 10,
                                },
                            ),
                        )
                    elif method == "tasks/cancel" and service.mcp_live_task_id is not None:
                        self._reply(
                            200,
                            _remote_reply(
                                request_payload.get("id"),
                                {"resultType": "complete"},
                            ),
                        )
                    elif service.mcp_failures:
                        status, payload = service.mcp_failures.pop(0)
                        self._reply(status, payload)
                    elif service.mcp_raw_responses:
                        self._reply_raw(200, service.mcp_raw_responses.pop(0))
                    elif service.mcp_responses:
                        self._reply(200, service.mcp_responses.pop(0))
                    else:
                        self._reply(
                            200,
                            {
                                "jsonrpc": "2.0",
                                "id": request_payload.get("id"),
                                "result": {
                                    "supportedVersions": ["2026-07-28"],
                                    "capabilities": {
                                        "tools": {"listChanged": False},
                                        "extensions": {"io.modelcontextprotocol/tasks": {}},
                                    },
                                    "_meta": {
                                        "io.modelcontextprotocol/serverInfo": {
                                            "name": "recurse",
                                            "version": "0.1.0",
                                        }
                                    },
                                },
                            },
                        )
                    return
                if path == "/artifact-download":
                    service.artifact_download_authorization = self.headers.get("Authorization")
                    service.artifact_download_user_agent = self.headers.get("User-Agent")
                    self._reply_raw(200, service.artifact_bytes)
                    return
                if path == "/direct-upload":
                    service.direct_upload_content_type = content_type
                    service.direct_upload_user_agent = self.headers.get("User-Agent")
                token = self.headers.get("Authorization")
                body: dict[str, Any] | bytes | None
                body = json.loads(raw) if content_type == "application/json" else raw or None
                service.requests.append((self.command, path, body, token))
                if path == "/direct-upload" and service.direct_upload_response_losses:
                    service.direct_upload_response_losses -= 1
                    assert token is not None
                    service.direct_upload_stored.add(token)
                    self.close_connection = True
                    return
                if path in service.fail_detail:
                    status, detail = service.fail_detail[path]
                    self._reply(status, {"detail": detail})
                    return
                if service.transient_failures.get(path, 0):
                    service.transient_failures[path] -= 1
                    self._reply(503, {"detail": "temporarily unavailable"})
                    return
                if path in service.malformed:
                    self._reply(200, service.malformed[path])
                    return
                self._reply(*service.route(self.command, path, body, token))

            # BaseHTTPRequestHandler requires these exact mixed-case method names.
            do_DELETE = do_GET = do_POST = do_PUT = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.registration: dict[str, Any] = {
            "agent_id": "agent-1",
            "version_id": "version-1",
            "version": 1,
            "source_upload": {
                "upload_id": "source-upload-9",
                "upload_url": f"{self.url}/direct-upload",
                "upload_authorization": "Bearer source-upload",
                "upload_content_type": "application/recurse-upload",
                "upload_prefix": base64.b64encode(b"source-prefix:").decode(),
                "upload_suffix": base64.b64encode(b":source-suffix").decode(),
            },
        }

    def close(self) -> None:
        """Shut the double down."""
        self.server.shutdown()
        self.server.server_close()

    def route(  # noqa: PLR0911,PLR0912 - one table-like public API double
        self, method: str, path: str, body: dict[str, Any] | bytes | None, token: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Answer one request following the public service contract."""
        if path == "/v1/auth/token":
            assert isinstance(body, dict)
            access_token = (
                self.token_responses.pop(0)
                if len(self.token_responses) > 1
                else self.token_responses[0]
            )
            response: dict[str, Any] = {
                "access_token": access_token,
                "token_type": "bearer",
                "expires_in": 900,
            }
            if body["grant_type"] == "authorization_code":
                response.update(
                    {
                        "device_credential": "device-1",
                        "credential_expires_at": "2027-08-31T00:00:00Z",
                    }
                )
            elif body["grant_type"] == "device_credential":
                self.device_grants.append(body["device_credential"])
            else:
                raise AssertionError(f"unexpected grant {body['grant_type']}")
            return 200, response
        if (method, path) == ("POST", "/v1/auth/logout"):
            assert body == {"device_credential": "device-1"}
            return 204, {}
        if (method, path) == ("POST", "/direct-upload"):
            assert token in {"Bearer source-upload", "Bearer lockfile-upload"}
            if self.direct_upload_failures:
                self.direct_upload_failures -= 1
                return 503, {"detail": "temporary transfer failure"}
            if token in self.direct_upload_stored:
                return 409, {"detail": "duplicate object id"}
            self.direct_upload_stored.add(token)
            return 201, {}
        assert token == f"Bearer {self.token_responses[0]}", (method, path)
        if path == "/v1/account":
            return 200, {"account_id": "a" * 32, "display_name": "Ada"}
        if (method, path) == ("POST", "/v1/runtime-secrets"):
            assert isinstance(body, dict)
            existing = next(
                (secret for secret in self.runtime_secrets if secret["name"] == body["name"]),
                None,
            )
            version = int(existing["active_version"]) + 1 if existing else 1
            metadata = {
                "secret_id": self.runtime_secret_id,
                "name": body["name"],
                "active_version": version,
                "created_at": "2026-09-03T12:00:00Z",
                "updated_at": "2026-09-03T13:00:00Z",
                "deployment_binding_count": 0,
            }
            self.runtime_secrets = [metadata]
            return 201, {
                "secret_id": self.runtime_secret_id,
                "name": body["name"],
                "version": version,
                "created_at": metadata["created_at"],
                "updated_at": metadata["updated_at"],
            }
        if (method, path) == ("GET", "/v1/runtime-secrets"):
            return 200, {"secrets": self.runtime_secrets}
        if (method, path) == (
            "DELETE",
            f"/v1/runtime-secrets/{self.runtime_secret_id}",
        ):
            self.runtime_secrets = []
            return 204, {}
        if (method, path) == ("POST", "/v1/agent-versions"):
            return 201, dict(self.registration)
        if (method, path) == ("POST", "/v1/agent-versions/version-1/complete"):
            if len(self.direct_upload_stored) != 1:
                return 409, {"detail": "application upload is incomplete"}
            return 202, {"version_id": "version-1", "status": "processing"}
        if (method, path) == ("GET", "/v1/agent-versions/version-1"):
            status = self.version_statuses.pop(0)
            error = self.version_error if status == "failed" else None
            return 200, {
                "version_id": "version-1",
                "version": 1,
                "status": status,
                "error": error,
                "model": self.version_model,
            }
        if (method, path) == ("POST", "/v1/deployments"):
            assert isinstance(body, dict)
            return 201, {
                "deployment_id": "mcp_1234",
                "endpoint": "https://mcp.recurse.run/mcp/mcp_1234",
                "cpu_limit": body["cpu_limit"],
                "memory_limit_mib": body["memory_limit_mib"],
            }
        if (method, path) == ("POST", "/v1/runs"):
            return 202, {"run_id": self.run_id, "status": "queued"}
        if (method, path) == ("GET", f"/v1/runs/{self.run_id}"):
            view = self.run_views.pop(0) if len(self.run_views) > 1 else self.run_views[0]
            return 200, view
        if (method, path) == ("POST", f"/v1/runs/{self.run_id}/cancel"):
            return 200, {"run_id": self.run_id, "status": "cancelled"}
        if method == "GET" and path.startswith("/v1/runs/") and "/artifacts/" in path:
            return 200, {
                "output_id": path.rsplit("/", 1)[-1],
                "download_url": f"{self.url}/artifact-download",
                "download_token": "artifact-token",
                "size_bytes": len(self.artifact_bytes),
                "sha256": self.artifact_sha256,
            }
        if (method, path) == ("GET", "/v1/billing"):
            return 200, {
                "balance_microusd": self.wallet_balance_microusd,
                "available_balance_microusd": self.wallet_available_microusd,
            }
        if (method, path) == ("POST", "/v1/billing/checkout"):
            return 201, {"checkout_url": "https://billing.recurse.run/checkout/session-1"}
        if (method, path) == ("POST", "/v1/billing/credit-codes/redeem"):
            self.wallet_balance_microusd = 10_000_000
            self.wallet_available_microusd = 9_921_276
            return 200, {
                "balance_microusd": self.wallet_balance_microusd,
                "available_balance_microusd": self.wallet_available_microusd,
            }
        raise AssertionError(f"unexpected request {method} {path}")


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeService]:
    """A running service double wired into RECURSE_API_URL."""
    fake = FakeService()
    monkeypatch.setenv("RECURSE_API_URL", fake.url)
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _ephemeral_callback_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Bind the login callback to an ephemeral port so tests never collide."""
    monkeypatch.setattr("_recurse_cli._CALLBACK_PORT", 0)
    monkeypatch.setattr(cli, "_AUTH_LOCK_DIRECTORY", tmp_path / "locks")


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """An in-memory keychain replacing the OS keyring."""
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        "keyring.set_password",
        lambda system, name, value: store.__setitem__((system, name), value),
    )
    monkeypatch.setattr("keyring.get_password", lambda system, name: store.get((system, name)))
    monkeypatch.setattr(
        "keyring.delete_password",
        lambda system, name: store.pop((system, name)),
    )
    return store


def _credential_key(api_url: str) -> tuple[str, str]:
    """Return the endpoint-scoped keychain key used by the CLI."""
    return "recurse-cli", f"device-credential:{api_url}"


@pytest.fixture
def logged_in(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    """A keychain already holding a durable device credential."""
    keychain[_credential_key(service.url)] = "device-1"
    return keychain


def _browser_completing_login(query: dict[str, str] | None = None) -> Callable[[str], bool]:
    """Build a fake browser that immediately completes the login callback."""

    def open_browser(url: str) -> bool:
        """Follow the login URL and call back with the given query."""
        parsed = urllib.parse.urlparse(url)
        request = dict(urllib.parse.parse_qsl(parsed.query))
        challenge = request["code_challenge"]
        assert len(challenge) == 43
        callback = dict(query or {"code": "code-1", "state": request["state"]})
        with urllib.request.urlopen(  # noqa: S310 - test-generated loopback URL
            request["redirect_to"] + "?" + urllib.parse.urlencode(callback)
        ) as response:
            assert response.status == 200
        return True

    return open_browser


def test_login_completes_pkce_and_stores_only_the_device_credential(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A full PKCE round trip stores only the durable device credential."""
    monkeypatch.setattr("webbrowser.open", _browser_completing_login())

    assert main(["login"]) == 0

    assert keychain == {_credential_key(service.url): "device-1"}
    assert all("refresh" not in name for _, name in keychain)
    exchange = next(body for _, path, body, _ in service.requests if path == "/v1/auth/token")
    assert isinstance(exchange, dict)
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code"] == "code-1"
    verifier = exchange["code_verifier"]
    assert len(verifier) == 43
    assert (
        len(base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")) == 43
    )
    output = capsys.readouterr().out
    assert f"Logged in to Recurse at {service.url} as Ada." in output
    assert f"Account: {'a' * 32}" in output


def test_device_credentials_are_scoped_to_the_api_endpoint(
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEV and PROD logins coexist without overwriting one another."""
    monkeypatch.setenv("RECURSE_API_URL", "https://api.dev.example.test")
    cli._store_device_credential({"device_credential": "device-dev"})

    monkeypatch.setenv("RECURSE_API_URL", "https://api.recurse.run")
    cli._store_device_credential({"device_credential": "device-prod"})
    assert cli._device_credential() == "device-prod"

    monkeypatch.setenv("RECURSE_API_URL", "https://api.dev.example.test")
    assert cli._device_credential() == "device-dev"


def test_login_rejects_a_callback_with_the_wrong_state(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A forged state never completes the login or stores a credential."""
    monkeypatch.setattr(
        "webbrowser.open", _browser_completing_login({"code": "code-1", "state": "forged"})
    )
    monkeypatch.setattr("_recurse_cli._LOGIN_WAIT_SECONDS", 1)

    assert main(["login"]) == 2

    assert keychain == {}
    assert "login was not completed" in capsys.readouterr().err


def test_login_reports_a_denied_authorization(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An error callback without a code leaves the keychain untouched."""
    monkeypatch.setattr("webbrowser.open", _browser_completing_login({"error": "access_denied"}))
    monkeypatch.setattr("_recurse_cli._LOGIN_WAIT_SECONDS", 1)

    assert main(["login"]) == 2
    assert keychain == {}


def test_login_times_out_without_a_callback(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Login fails cleanly when the browser never calls back."""
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    monkeypatch.setattr("_recurse_cli._LOGIN_WAIT_SECONDS", 0)

    assert main(["login"]) == 2
    output = capsys.readouterr()
    assert "Waiting up to 5 minutes" in output.out
    assert "login timed out" in output.err


def test_login_interrupt_stops_callback_server_without_saving_credentials(
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Interrupting browser login releases its listener and background thread."""
    servers: list[_LoginServer] = []
    stopped = threading.Event()

    class LoginServer(_LoginServer):
        """Observe real callback-server shutdown, including exceptional exit."""

        def __init__(self, state: str) -> None:
            """Keep the real listener available for cleanup assertions."""
            super().__init__(state)
            servers.append(self)

        def serve_forever(self, poll_interval: float = 0.5) -> None:
            """Record when the actual serving loop has exited."""
            try:
                super().serve_forever(poll_interval)
            finally:
                stopped.set()

    def interrupt_browser(_url: str) -> bool:
        """Interrupt after the callback serving thread has started."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_LoginServer", LoginServer)
    monkeypatch.setattr(webbrowser, "open", interrupt_browser)
    try:
        try:
            exit_status = main(["login"])
        except KeyboardInterrupt:
            pytest.fail("login leaked KeyboardInterrupt instead of returning 130")
        assert exit_status == 130
        assert stopped.wait(1)
        assert servers[0].fileno() == -1
        assert keychain == {}
        assert "Interrupted" in capsys.readouterr().err
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_run_interrupt_before_admission_stops_without_starting_a_run(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stopping preparation ends local waiting, without admitting or cancelling a run."""
    app = write_app(tmp_path / "app")

    def interrupt(_seconds: float) -> None:
        """Interrupt the real preparation-polling path."""
        raise KeyboardInterrupt

    monkeypatch.setattr("_recurse_cli.time.sleep", interrupt)
    try:
        exit_status = main(["run", str(app)])
    except KeyboardInterrupt:
        pytest.fail("preparation leaked KeyboardInterrupt instead of returning 130")
    assert exit_status == 130
    assert not any(path.startswith("/v1/runs") for _, path, _, _ in service.requests)
    assert "Interrupted" in capsys.readouterr().err


def test_deploy_builds_uploads_and_prints_only_public_results(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deploy runs the exact public request sequence and prints only id and endpoint."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["deploy", str(app), "--as", "mcp"]) == 0

    out = capsys.readouterr().out
    assert out.splitlines() == [
        "Packaging application...",
        "Uploading source distribution...",
        "Preparing runtime...",
        "Creating MCP deployment...",
        "deployment: mcp_1234",
        "endpoint: https://mcp.recurse.run/mcp/mcp_1234",
        "resources: 1 CPU, 1024 MiB",
    ]
    methods_and_paths = [(method, path) for method, path, _, _ in service.requests]
    assert methods_and_paths == [
        ("POST", "/v1/auth/token"),
        ("POST", "/v1/agent-versions"),
        ("POST", "/direct-upload"),
        ("POST", "/v1/agent-versions/version-1/complete"),
        ("GET", "/v1/agent-versions/version-1"),
        ("GET", "/v1/agent-versions/version-1"),
        ("POST", "/v1/deployments"),
    ]
    assert service.device_grants == ["device-1"]

    assert logged_in[_credential_key(service.url)] == "device-1"
    cached = json.loads(logged_in[("recurse-cli", f"access-token:{service.url}")])
    assert cached["access_token"] == "access-1"  # noqa: S105 - fake bearer
    registration = service.requests[1][2]
    assert isinstance(registration, dict)
    assert registration["agent_name"] == "receipt-writer"
    assert registration["summary"] == "Writes one receipt file."
    application_record = registration["application_record"]
    assert set(application_record) == {"apiVersion", "source"}
    assert application_record["apiVersion"] == "recurse.application/v1alpha1"
    source_upload = service.requests[2][2]
    assert isinstance(source_upload, bytes)
    assert source_upload.startswith(b"source-prefix:")
    assert source_upload.endswith(b":source-suffix")
    transferred_source = source_upload.removeprefix(b"source-prefix:").removesuffix(
        b":source-suffix"
    )
    assert hashlib.sha256(transferred_source).hexdigest() == application_record["source"]["sha256"]
    assert service.direct_upload_content_type == "application/recurse-upload"
    assert service.direct_upload_user_agent == "recurse-sdk"
    completion = service.requests[3][2]
    assert isinstance(completion, dict)
    assert completion == {
        "source_upload_id": "source-upload-9",
    }
    assert service.requests[-1][2] == {
        "version_id": "version-1",
        "cpu_limit": 1.0,
        "memory_limit_mib": 1024,
    }


def test_run_prepares_and_waits_for_one_direct_run(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run uses the prepared version directly and prints its terminal result."""
    app = write_app(tmp_path / "app")
    inputs = tmp_path / "inputs.json"
    inputs.write_text('{"message":"hello"}')
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    working = service.run_views[0] | {"status": "running"}
    service.run_views = [working] * 300 + [service.run_views[-1]]

    assert (
        main(
            [
                "run",
                str(app),
                "--inputs",
                str(inputs),
                "--cpu",
                "2",
                "--memory-mib",
                "2048",
            ]
        )
        == 0
    )

    registration = next(
        body for method, path, body, _ in service.requests if path == "/v1/agent-versions"
    )
    assert isinstance(registration, dict)
    application_record = registration["application_record"]
    assert set(application_record) == {"apiVersion", "source"}
    assert application_record["apiVersion"] == "recurse.application/v1alpha1"
    admission = next(body for method, path, body, _ in service.requests if path == "/v1/runs")
    assert isinstance(admission, dict)
    assert admission == {
        "version_id": "version-1",
        "idempotency_key": admission["idempotency_key"],
        "inputs": {"message": "hello"},
        "timeout_seconds": 900,
        "cpu_limit": 2.0,
        "memory_limit_mib": 2048,
    }
    assert admission["idempotency_key"].startswith("run_")
    assert not any(path == "/v1/deployments" for _, path, _, _ in service.requests)
    assert capsys.readouterr().out.splitlines() == [
        "Packaging application...",
        "Uploading source distribution...",
        "Preparing runtime...",
        f"run: {service.run_id}",
        "status: succeeded",
        "answer: done",
        "artifacts: 1",
    ]


def test_run_resolves_secret_names_before_preparation_and_admits_ids(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Removing pre-prepare resolution would send names or start billing too early."""
    del logged_in
    app = write_app(tmp_path / "app")
    service.runtime_secrets = [_runtime_secret_metadata(service)]
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert (
        main(
            [
                "run",
                str(app),
                "--secret",
                "GITHUB_TOKEN=github-token",
            ]
        )
        == 0
    )
    paths = [path for _method, path, _body, _token in service.requests]
    assert paths.index("/v1/runtime-secrets") < paths.index("/v1/agent-versions")
    admission = next(body for _, path, body, _ in service.requests if path == "/v1/runs")
    assert isinstance(admission, dict)
    assert admission["secret_bindings"] == {
        "GITHUB_TOKEN": service.runtime_secret_id,
    }
    assert not any(path == "/v1/deployments" for path in paths)
    assert capsys.readouterr().out.splitlines()[0] == ("secret binding: GITHUB_TOKEN=github-token")


def test_deploy_resolves_secret_names_before_preparation_and_binds_ids(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dropping deployment bindings would create an uncredentialed MCP target."""
    del logged_in
    app = write_app(tmp_path / "app")
    service.runtime_secrets = [_runtime_secret_metadata(service)]
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert (
        main(
            [
                "deploy",
                str(app),
                "--as",
                "mcp",
                "--secret",
                "GITHUB_TOKEN=github-token",
            ]
        )
        == 0
    )
    paths = [path for _method, path, _body, _token in service.requests]
    assert paths.index("/v1/runtime-secrets") < paths.index("/v1/agent-versions")
    deployment = next(body for _, path, body, _ in service.requests if path == "/v1/deployments")
    assert isinstance(deployment, dict)
    assert deployment["secret_bindings"] == {
        "GITHUB_TOKEN": service.runtime_secret_id,
    }
    assert capsys.readouterr().out.splitlines()[0] == ("secret binding: GITHUB_TOKEN=github-token")


@pytest.mark.parametrize(
    ("operation", "billing_retry_target"),
    [("deploy", "deployment"), ("run", "the run")],
)
def test_commands_reuse_the_token_that_resolved_secret_bindings(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    billing_retry_target: str,
) -> None:
    """Preparation must not exchange the device credential a second time."""
    monkeypatch.setattr(
        cli,
        "_resolve_runtime_secret_bindings",
        lambda _bindings: ("resolved-token", {"TOKEN": "secret-id"}),
    )
    prepared_with: list[tuple[str | None, str]] = []

    def prepare(
        _directory: str,
        *,
        token: str | None = None,
        billing_retry_target: str = "deployment",
    ) -> tuple[str, str]:
        """Stop after recording the shared preparation arguments."""
        prepared_with.append((token, billing_retry_target))
        raise RuntimeError("preparation observed")

    monkeypatch.setattr(cli, "_prepare", prepare)
    with pytest.raises(RuntimeError, match="preparation observed"):
        if operation == "deploy":
            cli._deploy("app", 1.0, 1024, ["TOKEN=secret"])
        else:
            cli._run("app", None, 1.0, 1024, ["TOKEN=secret"])

    assert prepared_with == [("resolved-token", billing_retry_target)]


@pytest.mark.parametrize(
    "binding",
    [
        "missing-separator",
        "=github-token",
        "1TOKEN=github-token",
        "RECURSE_INPUTS=github-token",
        "TOKEN=missing-secret",
    ],
)
def test_secret_binding_rejects_invalid_or_unresolved_mappings_before_preparation(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    binding: str,
) -> None:
    """Invalid authorization must not reach bundle upload or provider preparation."""
    del logged_in
    app = write_app(tmp_path / "app")
    service.runtime_secrets = [_runtime_secret_metadata(service)]

    assert main(["run", str(app), "--secret", binding]) == 2
    assert not any(path == "/v1/agent-versions" for _, path, _, _ in service.requests)
    assert "secret" in capsys.readouterr().err


def test_secret_binding_rejects_duplicate_environment_names_before_preparation(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repeated environment name cannot silently replace an earlier authorization."""
    del logged_in
    app = write_app(tmp_path / "app")
    service.runtime_secrets = [_runtime_secret_metadata(service)]

    assert (
        main(
            [
                "run",
                str(app),
                "--secret",
                "TOKEN=github-token",
                "--secret",
                "TOKEN=github-token",
            ]
        )
        == 2
    )
    assert not any(path == "/v1/agent-versions" for _, path, _, _ in service.requests)
    assert "duplicate" in capsys.readouterr().err


def test_run_retries_admission_with_the_same_idempotency_key(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retryable admission failure cannot create a second logical run."""
    app = write_app(tmp_path / "app")
    service.transient_failures["/v1/runs"] = 1
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["run", str(app)]) == 0

    admissions = [body for _, path, body, _ in service.requests if path == "/v1/runs"]
    assert len(admissions) == 2
    assert admissions[0] == admissions[1]


def test_run_reauthenticates_once_after_401_during_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long preparation can refresh auth without changing run admission."""
    run_id = "77777777-7777-4777-8777-777777777777"
    admissions: list[tuple[str, dict[str, Any]]] = []

    monkeypatch.setattr(cli, "_prepare", lambda _app, **_kwargs: ("access-old", "version-1"))
    monkeypatch.setattr(cli, "_access_token", lambda rejected_token=None: "access-new")
    monkeypatch.setattr(cli, "_POLL_SECONDS", 0)

    def respond(
        method: str,
        path: str,
        *,
        token: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Expire the prepared token on the first admission request."""
        del method
        if path == "/v1/runs":
            assert json_body is not None
            admissions.append((token, json_body))
            if token == "access-old":  # noqa: S105 - simulated access token
                raise cli.ServiceError("access token expired", 401)
            return {"run_id": run_id, "status": "queued"}
        return {
            "run_id": run_id,
            "status": "succeeded",
            "result": {"answer": "done"},
            "error": None,
            "artifacts": [],
            "payload_expired": False,
        }

    monkeypatch.setattr(cli, "_retry_request", respond)

    assert cli._run("app", None, 1.0, 1024) == 0
    assert [token for token, _body in admissions] == ["access-old", "access-new"]
    assert admissions[0][1] == admissions[1][1]


def test_run_continues_polling_after_a_transient_status_interruption(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status transport failures never resubmit or abandon the admitted run."""
    app = write_app(tmp_path / "app")
    service.transient_failures[f"/v1/runs/{service.run_id}"] = 2
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["run", str(app)]) == 0
    assert len([path for _, path, _, _ in service.requests if path == "/v1/runs"]) == 1


def test_run_reauthenticates_once_after_401_during_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admitted run survives expiry of the access token used to prepare it."""
    run_id = "77777777-7777-4777-8777-777777777777"
    tokens: list[str] = []

    monkeypatch.setattr(cli, "_prepare", lambda _app, **_kwargs: ("access-old", "version-1"))
    monkeypatch.setattr(cli, "_access_token", lambda rejected_token=None: "access-new")
    monkeypatch.setattr(cli, "_POLL_SECONDS", 0)

    def respond(
        method: str,
        path: str,
        *,
        token: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Expire the prepared token on the first status request."""
        del method, json_body
        if path == "/v1/runs":
            return {"run_id": run_id, "status": "queued"}
        tokens.append(token)
        if token == "access-old":  # noqa: S105 - simulated access token
            raise cli.ServiceError("access token expired", 401)
        return {
            "run_id": run_id,
            "status": "succeeded",
            "result": {"answer": "done"},
            "error": None,
            "artifacts": [],
            "payload_expired": False,
        }

    monkeypatch.setattr(cli, "_retry_request", respond)

    assert cli._run("app", None, 1.0, 1024) == 0
    assert tokens == ["access-old", "access-new"]


@pytest.mark.parametrize("cancel_status", ["cancelled", "succeeded", "failed", "running", "queued"])
def test_run_interrupt_cancels_and_reports_confirmed_state(  # noqa: PLR0913, PLR0917 - fixtures plus state
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cancel_status: str,
) -> None:
    """Ctrl-C requests cancellation, without claiming pending work has stopped."""
    app = write_app(tmp_path / "app")
    service.version_statuses = ["ready"]
    cancel_path = f"/v1/runs/{service.run_id}/cancel"
    service.malformed[cancel_path] = {"run_id": service.run_id, "status": cancel_status}
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    def interrupt(_seconds: float) -> None:
        """Interrupt only the direct-run polling sleep."""
        raise KeyboardInterrupt

    monkeypatch.setattr("_recurse_cli.time.sleep", interrupt)

    assert main(["run", str(app)]) == 130

    output = capsys.readouterr().out
    assert f"run: {service.run_id}" in output
    assert f"status: {cancel_status}" in output
    assert sum(path == cancel_path for _, path, _, _ in service.requests) == 1
    assert "detached" not in output
    if cancel_status in {"running", "queued"}:
        assert "may continue" in output
        assert f"recurse status {service.run_id}" in output


@pytest.mark.parametrize("failure", ["transport", "authentication", "malformed", "interrupt"])
def test_run_interrupt_preserves_recovery_when_cancellation_is_unconfirmed(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """An unsuccessful cancellation never masquerades as stopped remote work."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    original = cli.request

    def interrupted_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Interrupt polling, then exercise the selected cancellation boundary."""
        if method == "GET" and path == f"/v1/runs/{service.run_id}":
            raise KeyboardInterrupt
        if path.endswith("/cancel"):
            if failure == "interrupt":
                raise KeyboardInterrupt
            if failure == "transport":
                raise cli.ServiceError("connection lost")
            if failure == "authentication":
                raise cli.ServiceError("login expired", 401)
            return {"run_id": "wrong-run", "status": "cancelled"}
        return original(method, path, **kwargs)

    monkeypatch.setattr(cli, "request", interrupted_request)

    assert main(["run", "app"]) == 130

    output = capsys.readouterr()
    assert "may continue" in output.out
    assert output.out.splitlines().count(f"run: {service.run_id}") == 1
    assert f"recurse cancel {service.run_id}" in output.out
    assert f"recurse status {service.run_id}" in output.out
    assert "status: cancelled" not in output.out
    assert "Traceback" not in output.err


def test_run_interrupt_during_admission_reuses_the_exact_request(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lost admission response is recovered with the same idempotency key before cancellation."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    original = cli.request
    interrupted = False

    def lose_admission(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Interrupt only after the local service has accepted the first admission."""
        nonlocal interrupted
        response = original(method, path, **kwargs)
        if method == "POST" and path == "/v1/runs" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return response

    monkeypatch.setattr(cli, "request", lose_admission)

    assert main(["run", "app", "--cpu", "2"]) == 130

    admissions = [body for method, path, body, _ in service.requests if path == "/v1/runs"]
    assert len(admissions) == 2
    assert admissions[0] == admissions[1]
    assert sum(path.endswith("/cancel") for _, path, _, _ in service.requests) == 1
    assert f"run: {service.run_id}" in capsys.readouterr().out


@pytest.mark.parametrize("failure", [cli.ServiceError("offline"), KeyboardInterrupt()])
def test_run_interrupt_with_unknown_admission_preserves_uncertainty(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
) -> None:
    """If admission recovery also fails, retain its reference without claiming no run exists."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    attempts: list[dict[str, Any]] = []

    def interrupt_admission(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Keep both attempted request bodies while losing their responses."""
        assert (method, path) == ("POST", "/v1/runs")
        attempts.append(kwargs["json_body"])
        if len(attempts) == 1:
            raise KeyboardInterrupt
        raise failure

    monkeypatch.setattr(cli, "_run_request", interrupt_admission)

    assert main(["run", "app"]) == 130

    output = capsys.readouterr().out
    assert "may continue" in output
    assert attempts[0]["idempotency_key"] in output
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert "No run was started" not in output


def test_status_cancel_and_artifacts_use_the_public_run_routes(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Follow-up commands inspect, cancel, and verify retained artifact bytes."""
    service.run_views = [service.run_views[-1]]
    output_directory = tmp_path / "downloads"

    assert main(["status", service.run_id]) == 0
    assert main(["cancel", service.run_id]) == 0
    assert main(["artifacts", service.run_id, "--output", str(output_directory)]) == 0

    assert (output_directory / "results" / "receipt.json").read_bytes() == service.artifact_bytes
    output = capsys.readouterr().out
    assert "status: succeeded" in output
    assert "status: cancelled" in output
    assert "downloaded: results/receipt.json" in output
    assert service.artifact_download_authorization == "Bearer artifact-token"


def test_artifacts_replace_an_existing_regular_file_on_each_download(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
) -> None:
    """A verified artifact replaces only its destination and can be downloaded again."""
    service.run_views = [service.run_views[-1]]
    output_directory = tmp_path / "downloads"
    destination = output_directory / "results" / "receipt.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old artifact")
    unrelated = output_directory / "notes.txt"
    unrelated.write_bytes(b"keep me")

    assert main(["artifacts", service.run_id, "--output", str(output_directory)]) == 0
    assert destination.read_bytes() == service.artifact_bytes
    assert unrelated.read_bytes() == b"keep me"

    assert main(["artifacts", service.run_id, "--output", str(output_directory)]) == 0
    assert destination.read_bytes() == service.artifact_bytes
    assert unrelated.read_bytes() == b"keep me"


@pytest.mark.parametrize("run_status", ["queued", "running", "succeeded"])
def test_status_without_failure_preserves_normal_output(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
    run_status: str,
) -> None:
    """Error-reporting changes leave ordinary status output alone."""
    service.run_views = [
        {**service.run_views[0], "status": run_status, "error_detail": "Not a terminal failure."}
    ]

    assert main(["status", service.run_id]) == 0

    output = capsys.readouterr()
    assert output.out.splitlines() == [f"status: {run_status}", "artifacts: 0"]
    assert output.err == ""


@pytest.mark.parametrize(
    ("run_status", "expected_exit"),
    [
        ("failed", 1),
        ("timed_out", 5),
        ("cancelled", 3),
        ("infrastructure_failed", 4),
    ],
)
def test_run_terminal_failure_has_a_stable_exit_status(  # noqa: PLR0913, PLR0917 - fixtures plus parameters
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
    expected_exit: int,
) -> None:
    """Each terminal failure maps to a documented process exit status."""
    app = write_app(tmp_path / "app")
    service.run_views = [
        {
            "run_id": service.run_id,
            "status": run_status,
            "result": None,
            "error": run_status,
            "artifacts": [],
            "payload_expired": False,
        }
    ]
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["run", str(app)]) == expected_exit


@pytest.mark.parametrize(
    "case",
    [
        ("failed", "insufficient_balance", 1, "recurse billing top-up 5"),
        ("failed", "secret_unavailable", 1, "recurse secret list"),
        ("failed", "invalid_inputs", 1, "input schema"),
        ("failed", "invalid_agent", 1, "agent.yaml"),
        ("failed", "invalid_output", 1, "output schema"),
        ("failed", "execution_failed", 1, "No further public cause"),
        ("failed", "artifact_failed", 1, "collected or stored"),
        ("timed_out", "timed_out", 5, "time limit"),
        ("cancelled", "cancelled", 3, "service reports"),
        ("infrastructure_failed", "infrastructure_failed", 4, "service reports"),
    ],
)
def test_run_failure_explains_the_confirmed_public_reason(  # noqa: PLR0913, PLR0917 - fixtures plus public contract cases
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str, int, str],
) -> None:
    """A confirmed failure retains identity, exit category, reason and useful guidance."""
    state, reason, exit_status, hint = case
    service.version_statuses = ["ready"]
    service.run_views = [{**service.run_views[0], "status": state, "error": reason}]
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["run", str(write_app(tmp_path / "app"))]) == exit_status

    output = capsys.readouterr()
    assert f"run: {service.run_id}" in output.out
    assert f"status: {state}" in output.out
    assert f"error: {reason}:" in output.out
    assert hint in output.out
    assert "may continue" not in output.out
    assert output.err == ""


@pytest.mark.parametrize("command", ["run", "status"])
@pytest.mark.parametrize(
    ("state", "exit_status"),
    [("failed", 1), ("timed_out", 5), ("cancelled", 3), ("infrastructure_failed", 4)],
)
def test_run_commands_display_public_failure_detail(  # noqa: PLR0913, PLR0917 - fixtures and CLI cases
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    state: str,
    exit_status: int,
) -> None:
    """Both CLI paths retain the public explanation, code, identity and exit behavior."""
    reason = "execution_failed" if state == "failed" else state
    detail = "The run could not finish. Check the task input before retrying."
    service.version_statuses = ["ready"]
    service.run_views = [
        {
            **service.run_views[0],
            "status": state,
            "error": reason,
            "error_detail": detail,
            "private_trace": "must-not-be-displayed",
        }
    ]
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    target = str(write_app(tmp_path / "app")) if command == "run" else service.run_id

    assert main([command, target]) == (exit_status if command == "run" else 0)

    output = capsys.readouterr().out
    assert f"run: {service.run_id}" in output
    assert f"status: {state}" in output
    assert f"error: {reason}: {detail}" in output
    assert "No further public cause" not in output
    assert "must-not-be-displayed" not in output
    assert sum(path == f"/v1/runs/{service.run_id}" for _, path, _, _ in service.requests) == 1


@pytest.mark.parametrize("detail", [None, "", " \t\n ", 42, {}, []])
def test_status_falls_back_when_public_detail_is_unusable(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
    detail: object,
) -> None:
    """Missing usable public text retains the existing safe explanation."""
    service.run_views = [
        {
            **service.run_views[0],
            "status": "failed",
            "error": "execution_failed",
            "error_detail": detail,
        }
    ]

    assert main(["status", service.run_id]) == 0

    output = capsys.readouterr().out
    assert f"run: {service.run_id}" in output
    assert "error: execution_failed: The agent did not complete successfully." in output
    assert "No further public cause is available." in output


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("  Check café input.  ", "Check café input."),
        ("Check\x1b[2J\r\b\x00\n\t\u202einput", r"Check\x1b[2J\r\x08\x00\n\t\u202einput"),
    ],
)
def test_status_escapes_controls_in_public_detail(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
    detail: str,
    expected: str,
) -> None:
    """Public text stays readable without executing terminal or directional controls."""
    service.run_views = [
        {
            **service.run_views[0],
            "status": "failed",
            "error": "execution_failed",
            "error_detail": detail,
        }
    ]

    assert main(["status", service.run_id]) == 0

    assert capsys.readouterr().out.splitlines() == [
        f"run: {service.run_id}",
        "status: failed",
        f"error: execution_failed: {expected}",
        "artifacts: 0",
    ]


@pytest.mark.parametrize("reason", ["private provider payload", {"private": "payload"}, None])
def test_unknown_run_failure_is_safe_and_keeps_identity(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
    reason: object,
) -> None:
    """An unrecognized failed-run reason is not echoed or given an invented cause."""
    service.run_views = [{**service.run_views[0], "status": "failed", "error": reason}]

    assert main(["status", service.run_id]) == 0

    output = capsys.readouterr().out
    assert f"run: {service.run_id}" in output
    assert "error: unknown_error:" in output
    assert "No further public cause" in output
    assert "private" not in output
    assert "payload" not in output


@pytest.mark.parametrize("failure", ["authentication", "transport", "malformed", "server"])
def test_failed_run_observation_preserves_identity_without_resubmission(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    """Losing observation must not look like a terminal run failure or start another run."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    path = f"/v1/runs/{service.run_id}"
    if failure == "authentication":
        service.fail_detail[path] = (401, "private authentication detail")
    elif failure == "malformed":
        service.malformed[path] = {"run_id": "wrong-id", "status": "failed"}
    elif failure == "server":
        service.fail_detail[path] = (500, "private server traceback")
    else:
        original = urllib.request.urlopen

        def disconnect(request: urllib.request.Request, **kwargs: Any) -> Any:
            """Lose only the status response at the actual HTTP client boundary."""
            if request.full_url.endswith(path):
                raise urllib.error.URLError("private network diagnostic")
            return original(request, **kwargs)

        monkeypatch.setattr(urllib.request, "urlopen", disconnect)

    assert main(["run", "app"]) == 2

    output = capsys.readouterr()
    assert f"recurse status {service.run_id}" in output.out
    assert f"recurse cancel {service.run_id}" in output.out
    assert "may continue" in output.out
    assert "before starting another run" in output.out
    assert "status: failed" not in output.out
    assert "private" not in output.err
    assert (
        "authentication_failed" in output.err
        if failure == "authentication"
        else "request_failed" in output.err
    )
    if failure == "authentication":
        assert "recurse login" in output.err
    assert sum(path == "/v1/runs" for _, path, _, _ in service.requests) == 1
    assert not any(path.endswith("/cancel") for _, path, _, _ in service.requests)


def test_lost_admission_response_retains_reference_without_retrying(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed acknowledgement can follow acceptance; preserve its key, not a retry prompt."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    original = cli.request

    def lose_response(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Let the service accept admission, then lose that response."""
        response = original(method, path, **kwargs)
        if path == "/v1/runs":
            raise cli.ServiceError("the Recurse service could not be reached")
        return response

    monkeypatch.setattr(cli, "request", lose_response)

    assert main(["run", "app"]) == 2

    output = capsys.readouterr().out
    admissions = [body for _, path, body, _ in service.requests if path == "/v1/runs"]
    assert len(admissions) == 1
    assert isinstance(admissions[0], dict)
    assert f"admission: {admissions[0]['idempotency_key']}" in output
    assert "may continue" in output
    assert "do not blindly retry" in output


def test_rejected_run_admission_does_not_report_uncertain_remote_state(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A confirmed input rejection cannot have started a run or incurred charges."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    service.fail_detail["/v1/runs"] = (422, "inputs do not match the tool schema")

    assert main(["run", "app"]) == 2

    output = capsys.readouterr()
    assert "request_failed: inputs do not match the tool schema" in output.err
    assert "Remote state is unconfirmed" not in output.out
    assert "Execution and charges may continue" not in output.out
    assert "admission:" not in output.out


def test_login_removed_during_run_observation_preserves_recovery(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Losing the local credential during reauthentication cannot hide the admitted run."""
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    service.fail_detail[f"/v1/runs/{service.run_id}"] = (401, "expired access token")
    logged_in.clear()

    assert main(["run", "app"]) == 2

    output = capsys.readouterr()
    assert "recurse login" in output.err
    assert "may continue" in output.out
    assert f"recurse status {service.run_id}" in output.out
    assert not any(path.endswith("/cancel") for _, path, _, _ in service.requests)


@pytest.mark.parametrize("command", ["status", "cancel"])
def test_follow_up_authentication_failure_keeps_run_recovery(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    """Failed authentication cannot establish whether a previously admitted run stopped."""
    service.fail_detail["/v1/auth/token"] = (401, "private authentication detail")

    assert main([command, service.run_id]) == 2

    output = capsys.readouterr()
    assert "authentication_failed" in output.err
    assert "recurse login" in output.err
    assert "private" not in output.err
    assert f"recurse status {service.run_id}" in output.out
    assert "may continue" in output.out


def test_cli_syntax_error_uses_cli_layer_error_status(
    service: FakeService, capsys: pytest.CaptureFixture[str]
) -> None:
    """Parser rejection is a CLI error, not a confirmed remote timeout."""
    with pytest.raises(SystemExit) as exit_info:
        main(["run"])
    assert exit_info.value.code == 2
    output = capsys.readouterr()
    assert "usage:" in output.err
    assert "status: timed_out" not in output.out
    assert service.requests == []


@pytest.mark.parametrize("content", ["[broken", "[]"])
def test_run_inputs_require_one_readable_json_object(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    content: str,
) -> None:
    """Syntax errors and non-object inputs fail before packaging."""
    source = tmp_path / "inputs.json"
    source.write_text(content)

    assert main(["run", str(tmp_path), "--inputs", str(source)]) == 2
    assert "readable JSON object" in capsys.readouterr().err


def test_run_inputs_can_be_read_from_standard_input(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dash consumes exactly one JSON object from stdin."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"message":"stdin"}'))

    assert main(["run", str(app), "--inputs", "-"]) == 0
    admission = next(body for _, path, body, _ in service.requests if path == "/v1/runs")
    assert isinstance(admission, dict)
    assert admission["inputs"] == {"message": "stdin"}


def _runtime_secret_metadata(service: FakeService) -> dict[str, Any]:
    """Return one valid metadata-only secret record for CLI tests."""
    return {
        "secret_id": service.runtime_secret_id,
        "name": "github-token",
        "active_version": 2,
        "created_at": "2026-09-03T12:00:00Z",
        "updated_at": "2026-09-03T13:00:00Z",
        "deployment_binding_count": 3,
    }


def test_secret_set_from_stdin_preserves_exact_utf8_bytes(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Standard input is decoded without trimming a trailing newline."""
    del logged_in
    monkeypatch.setattr(
        "sys.stdin",
        type("BinaryInput", (), {"buffer": io.BytesIO(b"private-token\n")})(),
    )

    assert main(["secret", "set", "github-token", "--from-stdin"]) == 0

    request_record = next(
        record for record in service.requests if record[:2] == ("POST", "/v1/runtime-secrets")
    )
    body = cast(dict[str, Any], request_record[2])
    assert body["name"] == "github-token"
    assert body["value"] == "private-token\n"
    assert body["idempotency_key"].startswith("secret_")
    output = capsys.readouterr().out
    assert output == "secret: github-token\nversion: 1\nupdated: 2026-09-03T13:00:00Z\n"
    assert "private-token" not in output


def test_secret_set_prompts_twice_without_echo(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive entry uses two hidden prompts and submits no echo."""
    del logged_in
    answers = iter(["özel-token", "özel-token"])
    prompts: list[str] = []

    def hidden_prompt(prompt: str) -> str:
        """Record and answer one hidden-value prompt."""
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("getpass.getpass", hidden_prompt)

    assert main(["secret", "set", "github-token"]) == 0

    assert prompts == ["Secret value: ", "Confirm secret value: "]
    body = cast(
        dict[str, Any],
        next(
            record[2]
            for record in service.requests
            if record[:2] == ("POST", "/v1/runtime-secrets")
        ),
    )
    assert body["value"] == "özel-token"


def test_secret_set_retries_with_the_same_idempotency_key(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambiguous set retry keeps one request identity and exact value."""
    del logged_in
    service.transient_failures["/v1/runtime-secrets"] = 1
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    monkeypatch.setattr("sys.stdin", type("BinaryInput", (), {"buffer": io.BytesIO(b"value")})())

    assert main(["secret", "set", "github-token", "--from-stdin"]) == 0

    bodies = [
        body
        for method, path, body, _token in service.requests
        if (method, path) == ("POST", "/v1/runtime-secrets")
    ]
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("INVALID", b"value", "lowercase"),
        ("valid-name", b"", "must not be empty"),
        ("valid-name", b"x" * 32769, "32 KiB"),
        ("valid-name", b"\xff", "valid UTF-8"),
    ],
)
def test_secret_set_rejects_invalid_name_or_stdin_value_before_network(  # noqa: PLR0913, PLR0917 - fixtures plus parameters
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: bytes,
    message: str,
) -> None:
    """Local validation prevents invalid values from reaching the service."""
    del logged_in
    monkeypatch.setattr("sys.stdin", type("BinaryInput", (), {"buffer": io.BytesIO(value)})())

    assert main(["secret", "set", name, "--from-stdin"]) == 2

    assert message in capsys.readouterr().err
    assert not any(
        path == "/v1/runtime-secrets" for _method, path, _body, _token in service.requests
    )


@pytest.mark.parametrize(
    "case",
    [
        pytest.param((("first", "second"), "do not match"), id="mismatch"),
        pytest.param((("\udc80", "\udc80"), "valid UTF-8"), id="invalid-utf8"),
    ],
)
def test_secret_set_rejects_invalid_hidden_values(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[tuple[str, str], str],
) -> None:
    """Invalid interactive input aborts before authentication."""
    del logged_in
    answers, message = case
    responses = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(responses))

    assert main(["secret", "set", "github-token"]) == 2

    assert message in capsys.readouterr().err
    assert not any(
        path == "/v1/runtime-secrets" for _method, path, _body, _token in service.requests
    )


def test_secret_list_prints_metadata_but_never_a_value(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Listing prints only non-value metadata returned by the service."""
    del logged_in
    service.runtime_secrets = [_runtime_secret_metadata(service)]

    assert main(["secret", "list"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "NAME\tVERSION\tMCP BINDINGS\tUPDATED",
        "github-token\t2\t3\t2026-09-03T13:00:00Z",
    ]


def test_secret_delete_shows_binding_impact_and_requires_confirmation(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Interactive deletion identifies affected persistent bindings."""
    del logged_in
    service.runtime_secrets = [_runtime_secret_metadata(service)]
    prompts: list[str] = []

    def confirm(prompt: str) -> str:
        """Record and accept the destructive confirmation."""
        prompts.append(prompt)
        return "yes"

    monkeypatch.setattr("builtins.input", confirm)

    assert main(["secret", "delete", "github-token"]) == 0

    assert prompts == ["Delete github-token and remove it from 3 MCP deployments? [y/N] "]
    assert service.runtime_secrets == []
    assert capsys.readouterr().out == "deleted: github-token\n"


def test_secret_delete_can_be_declined_or_confirmed_noninteractively(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deletion can be declined or explicitly confirmed for automation."""
    del logged_in
    service.runtime_secrets = [_runtime_secret_metadata(service)]
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert main(["secret", "delete", "github-token"]) == 0
    assert service.runtime_secrets
    assert capsys.readouterr().out == "not deleted: github-token\n"

    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("--yes must not prompt"),
    )
    assert main(["secret", "delete", "github-token", "--yes"]) == 0
    assert service.runtime_secrets == []


@pytest.mark.parametrize(
    ("interruption", "expected_status", "message"),
    [
        (EOFError(), 2, "error: secret deletion was cancelled"),
        (KeyboardInterrupt(), 130, "Interrupted"),
    ],
)
def test_secret_delete_reports_cancelled_confirmation(  # noqa: PLR0913, PLR0917 - fixtures plus expected outcome
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: BaseException,
    expected_status: int,
    message: str,
) -> None:
    """A closed or interrupted confirmation prompt produces a concise error."""
    del logged_in
    service.runtime_secrets = [_runtime_secret_metadata(service)]

    def interrupt(_prompt: str) -> str:
        """Raise the scripted terminal interruption."""
        raise interruption

    monkeypatch.setattr("builtins.input", interrupt)

    assert main(["secret", "delete", "github-token"]) == expected_status

    assert message in capsys.readouterr().err
    assert service.runtime_secrets


def test_secret_commands_offer_no_literal_value_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Secret literals cannot be supplied as process arguments."""
    with pytest.raises(SystemExit) as error:
        main(["secret", "set", "github-token", "--value", "private-token"])

    assert error.value.code == 2
    assert "unrecognized arguments: --value private-token" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("interruption", "expected_error"),
    [(EOFError(), cli._CliError), (KeyboardInterrupt(), KeyboardInterrupt)],
)
def test_secret_hidden_entry_reports_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
    expected_error: type[BaseException],
) -> None:
    """EOF aborts entry; a keyboard interrupt reaches the common exit-130 handler."""

    def interrupt(_prompt: str) -> str:
        """Raise the scripted terminal interruption."""
        raise interruption

    monkeypatch.setattr("getpass.getpass", interrupt)

    with pytest.raises(expected_error):
        cli._read_runtime_secret(from_stdin=False)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"value": "forbidden"},
        {"secret_id": "not-a-uuid"},
        {
            "secret_id": "22222222-2222-4222-8222-222222222222",
            "name": "INVALID",
            "active_version": 0,
            "created_at": "",
            "updated_at": "",
            "deployment_binding_count": -1,
        },
    ],
)
def test_secret_metadata_rejects_malformed_or_value_bearing_records(
    payload: object,
) -> None:
    """Secret responses must contain a complete metadata-only record."""
    with pytest.raises(cli.ServiceError, match="invalid secret metadata"):
        cli._runtime_secret_metadata(payload)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"value": "forbidden"},
        {"secret_id": "not-a-uuid"},
        {
            "secret_id": "22222222-2222-4222-8222-222222222222",
            "name": "INVALID",
            "version": 0,
            "created_at": "",
            "updated_at": "",
        },
    ],
)
def test_secret_version_rejects_malformed_or_value_bearing_records(
    payload: object,
) -> None:
    """Set responses must identify one complete metadata-only version."""
    with pytest.raises(cli.ServiceError, match="invalid secret metadata"):
        cli._runtime_secret_version_metadata(payload)


def test_secret_list_rejects_a_malformed_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The metadata endpoint must return a list under its stable field."""
    monkeypatch.setattr(cli, "request", lambda *_args, **_kwargs: {"secrets": {}})

    with pytest.raises(cli.ServiceError, match="invalid secret metadata"):
        cli._runtime_secrets("token")


def test_secret_set_rejects_a_different_returned_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful set response must identify the requested logical name."""
    response = {
        "secret_id": "22222222-2222-4222-8222-222222222222",
        "name": "other-token",
        "version": 1,
        "created_at": "2026-09-03T12:00:00Z",
        "updated_at": "2026-09-03T13:00:00Z",
    }
    monkeypatch.setattr(cli, "_read_runtime_secret", lambda **_kwargs: "value")
    monkeypatch.setattr(cli, "_access_token", lambda: "token")
    monkeypatch.setattr(cli, "_retry_request", lambda *_args, **_kwargs: response)

    with pytest.raises(cli.ServiceError, match="invalid secret metadata"):
        cli._secret_set("github-token", from_stdin=False)


def test_secret_delete_reports_a_missing_logical_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion resolves names account-locally before issuing a destructive request."""
    monkeypatch.setattr(cli, "_access_token", lambda: "token")
    monkeypatch.setattr(cli, "_runtime_secrets", lambda _token: [])

    with pytest.raises(cli._CliError, match="was not found"):
        cli._secret_delete("github-token", confirmed=True)


def test_non_transient_run_request_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only gateway unavailability is eligible for an idempotent retry."""
    attempts = 0

    def reject(*args: object, **kwargs: object) -> dict[str, Any]:
        """Reject one request with a permanent public error."""
        nonlocal attempts
        del args, kwargs
        attempts += 1
        raise cli.ServiceError("invalid", 422)

    monkeypatch.setattr(cli, "request", reject)
    with pytest.raises(cli.ServiceError, match="invalid"):
        cli._retry_request(
            "POST",
            "/v1/runs",
            token="token",  # noqa: S106 - inert authentication fixture
            json_body={},
        )
    assert attempts == 1


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "different"},
        {"status": "unknown"},
        {"artifacts": {}},
        {"payload_expired": "no"},
    ],
)
def test_run_status_rejects_malformed_public_fields(change: dict[str, Any]) -> None:
    """Follow-up commands do not act on ambiguous run state."""
    run_id = "77777777-7777-4777-8777-777777777777"
    view: dict[str, Any] = {
        "run_id": run_id,
        "status": "queued",
        "result": None,
        "error": None,
        "artifacts": [],
        "payload_expired": False,
    }
    view.update(change)
    with pytest.raises(cli.ServiceError, match="invalid run response"):
        cli._validated_run_view(view, run_id)


def test_run_status_accepts_the_canonical_form_of_an_uppercase_uuid() -> None:
    """A UUID spelling accepted by the API still matches its canonical response."""
    run_id = "77777777-7777-4777-8777-77777777777a"
    view: dict[str, Any] = {
        "run_id": run_id,
        "status": "queued",
        "result": None,
        "error": None,
        "artifacts": [],
        "payload_expired": False,
    }

    assert cli._validated_run_view(view, run_id.upper()) is view


def test_status_prints_structured_results_and_expiry(capsys: pytest.CaptureFixture[str]) -> None:
    """Non-answer results stay readable and expiry is explicit."""
    cli._print_run_view(
        {
            "status": "succeeded",
            "result": {"score": 0.9},
            "error": None,
            "artifacts": [],
            "payload_expired": True,
        }
    )
    assert capsys.readouterr().out.splitlines() == [
        "status: succeeded",
        'result: {"score":0.9}',
        "artifacts: 0",
        "payloads: expired",
    ]


def test_failed_run_prints_the_wallet_recovery_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Insufficient balance tells a person or agent exactly how to recover."""
    cli._print_run_view(
        {
            "status": "failed",
            "result": None,
            "error": "insufficient_balance",
            "artifacts": [],
            "payload_expired": False,
        }
    )

    assert capsys.readouterr().out.splitlines() == [
        "status: failed",
        (
            "error: insufficient_balance: Wallet balance is too low. "
            "Add balance with `recurse billing top-up 5` or redeem a code with "
            "`recurse billing redeem CODE`, then retry."
        ),
        "artifacts: 0",
    ]


def test_run_rejects_malformed_admission_and_poll_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission acknowledgement and bounded polling both fail clearly."""
    monkeypatch.setattr(cli, "_prepare", lambda _app, **_kwargs: ("token", "version"))
    monkeypatch.setattr(
        cli,
        "_retry_request",
        lambda *args, **kwargs: {"run_id": "run-id", "status": "running"},
    )
    with pytest.raises(cli.ServiceError, match="invalid run response"):
        cli._run("app", None, 1.0, 1024)

    monkeypatch.setattr(
        cli,
        "_retry_request",
        lambda *args, **kwargs: {"run_id": "run-id", "status": "queued"},
    )
    monkeypatch.setattr(cli, "_RUN_POLL_ATTEMPTS", 0)
    with pytest.raises(cli._CliError, match="did not finish"):
        cli._run("app", None, 1.0, 1024)

    monkeypatch.setattr(cli, "_RUN_POLL_ATTEMPTS", 1)

    def missing_run(
        method: str,
        path: str,
        *,
        token: str,
        json_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Admit successfully, then reject the status lookup."""
        del method, json_body
        if path == "/v1/runs":
            return {"run_id": "run-id", "status": "queued"}, token
        raise cli.ServiceError("run was not found", 404)

    monkeypatch.setattr(
        cli,
        "_run_request",
        missing_run,
    )
    with pytest.raises(cli.ServiceError, match="run was not found"):
        cli._run("app", None, 1.0, 1024)


@pytest.mark.parametrize(
    "response",
    [
        {"run_id": "different", "status": "cancelled"},
        {"run_id": "run-id", "status": "unknown"},
    ],
)
def test_cancel_rejects_a_malformed_confirmation(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]
) -> None:
    """Cancellation prints nothing unless identity and state are valid."""
    monkeypatch.setattr(cli, "_access_token", lambda: "token")
    monkeypatch.setattr(cli, "_retry_request", lambda *args, **kwargs: response)
    with pytest.raises(cli.ServiceError, match="invalid run response"):
        cli._cancel("run-id")


@pytest.mark.parametrize("path", [None, "../secret", "/absolute", "a\\b", "a//b"])
def test_artifact_paths_cannot_escape_or_be_ambiguous(tmp_path: Path, path: object) -> None:
    """Artifact metadata is always resolved beneath the selected directory."""
    with pytest.raises(cli.ServiceError, match="invalid artifact metadata"):
        cli._artifact_path(tmp_path, path)


def test_artifact_path_cannot_escape_through_a_local_symlink(tmp_path: Path) -> None:
    """A safe-looking service path cannot follow a nested link outside the target."""
    output = tmp_path / "output"
    outside = tmp_path / "outside"
    output.mkdir()
    outside.mkdir()
    (output / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(cli.ServiceError, match="invalid artifact metadata"):
        cli._artifact_path(output, "linked/result.json")


def test_artifacts_reject_a_destination_symlink_without_replacing_its_target(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
) -> None:
    """An inventoried artifact path cannot alias and replace an unrelated local file."""
    service.run_views = [service.run_views[-1]]
    output_directory = tmp_path / "downloads"
    unrelated = output_directory / "notes.txt"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"keep me")
    destination = output_directory / "results" / "receipt.json"
    destination.parent.mkdir()
    destination.symlink_to("../notes.txt")

    assert main(["artifacts", service.run_id, "--output", str(output_directory)]) == 2
    assert destination.is_symlink()
    assert unrelated.read_bytes() == b"keep me"


@pytest.mark.parametrize("case", ["expired", "non-object", "directory"])
def test_artifact_download_refuses_unusable_metadata_or_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    """Expired payloads, malformed lists, and destination directories remain errors."""
    monkeypatch.setattr(cli, "_access_token", lambda: "token")
    artifact: object = {
        "output_id": "output-id",
        "path": "result.json",
    }
    destination = tmp_path / "result.json"
    if case == "non-object":
        artifact = "bad"
    elif case == "directory":
        destination.mkdir()
    monkeypatch.setattr(
        cli,
        "_run_request",
        lambda *_args, **_kwargs: (
            {
                "run_id": "run-id",
                "status": "succeeded",
                "result": {"answer": "done"},
                "error": None,
                "payload_expired": case == "expired",
                "artifacts": [artifact],
            },
            "token",
        ),
    )
    with pytest.raises((cli.ServiceError, cli._CliError)):
        cli._artifacts("run-id", str(tmp_path))
    if case == "directory":
        assert destination.is_dir()
        assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    "failure",
    [
        cli.ServiceError("artifact download failed"),
        cli.ServiceError("artifact verification failed"),
    ],
)
def test_artifact_download_failure_preserves_an_existing_file(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: cli.ServiceError,
) -> None:
    """Transport and verification failures cannot replace previously downloaded bytes."""
    service.run_views = [service.run_views[-1]]
    destination = tmp_path / "results" / "receipt.json"
    destination.parent.mkdir()
    destination.write_bytes(b"known good artifact")
    monkeypatch.setattr(cli, "_download_artifact", lambda _grant: (_ for _ in ()).throw(failure))

    with pytest.raises(cli.ServiceError, match=str(failure)):
        cli._artifacts(service.run_id, str(tmp_path))
    assert destination.read_bytes() == b"known good artifact"
    assert list(destination.parent.iterdir()) == [destination]


def test_artifact_atomic_write_cleans_up_after_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed final rename is concise and leaves no partial bytes behind."""
    monkeypatch.setattr(cli, "_access_token", lambda: "token")

    def run_request(
        method: str, path: str, *, token: str, **_kwargs: Any
    ) -> tuple[dict[str, Any], str]:
        """Return one complete run view from the safe status request."""
        del method, path
        return (
            {
                "run_id": "run-id",
                "status": "succeeded",
                "result": {"answer": "done"},
                "error": None,
                "payload_expired": False,
                "artifacts": [{"output_id": "output-id", "path": "result.json"}],
            },
            token,
        )

    monkeypatch.setattr(cli, "_run_request", run_request)
    monkeypatch.setattr(cli, "_authenticated_request", lambda *_args, **_kwargs: ({}, "token"))
    monkeypatch.setattr(cli, "_download_artifact", lambda _grant: b"result")
    destination = tmp_path / "result.json"
    destination.write_bytes(b"known good artifact")

    def fail_replace(source: str, destination: Path) -> None:
        """Simulate a filesystem failure after the temporary write."""
        del source, destination
        raise OSError("rename failed")

    monkeypatch.setattr("os.replace", fail_replace)
    assert main(["artifacts", "run-id", "--output", str(tmp_path)]) == 2
    assert "error: could not save artifact result.json: rename failed" in capsys.readouterr().err
    assert destination.read_bytes() == b"known good artifact"
    assert list(tmp_path.iterdir()) == [destination]


def test_deploy_sends_and_confirms_selected_resource_defaults(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deploy forwards explicit ceilings and prints the persisted defaults."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert (
        main(
            [
                "deploy",
                str(app),
                "--as",
                "mcp",
                "--cpu",
                "2.5",
                "--memory-mib",
                "4096",
            ]
        )
        == 0
    )

    assert service.requests[-1][2] == {
        "version_id": "version-1",
        "cpu_limit": 2.5,
        "memory_limit_mib": 4096,
    }
    assert "resources: 2.5 CPU, 4096 MiB" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["--cpu", "many"],
        ["--cpu", "0.2"],
        ["--cpu", "16.125"],
        ["--memory-mib", "1.5"],
        ["--memory-mib", "129"],
        ["--memory-mib", "16512"],
    ],
)
def test_deploy_rejects_invalid_resource_defaults(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    """Deploy rejects ceilings outside the supported grid before doing work."""
    app = write_app(tmp_path / "app")

    with pytest.raises(SystemExit):
        main(["deploy", str(app), "--as", "mcp", *arguments])


def test_deploy_rejects_malformed_resource_confirmation_before_printing_success(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Deploy prints no success fields until its resource confirmation is valid."""
    app = write_app(tmp_path / "app")
    service.malformed["/v1/deployments"] = {
        "deployment_id": "mcp_1234",
        "endpoint": "https://mcp.recurse.run/mcp/mcp_1234",
        "cpu_limit": True,
        "memory_limit_mib": 1024,
    }
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["deploy", str(app), "--as", "mcp"]) == 2

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        "Packaging application...",
        "Uploading source distribution...",
        "Preparing runtime...",
        "Creating MCP deployment...",
    ]
    assert "deployment:" not in captured.out
    assert "invalid response" in captured.err


def test_deploy_retries_the_same_direct_transfer_once(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient transfer failure reuses the exact opaque descriptor once."""
    app = write_app(tmp_path / "app")
    service.direct_upload_failures = 1
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["deploy", str(app), "--as", "mcp"]) == 0

    uploads = [request for request in service.requests if request[1] == "/direct-upload"]
    assert len(uploads) == 2
    assert uploads[0] == uploads[1]


def test_deploy_completes_when_a_landed_upload_loses_its_response(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completion resolves a lost success followed by a duplicate-upload response."""
    app = write_app(tmp_path / "app")
    service.direct_upload_response_losses = 1
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["deploy", str(app), "--as", "mcp"]) == 0

    paths = [path for _, path, _, _ in service.requests]
    assert paths[2:5] == [
        "/direct-upload",
        "/direct-upload",
        "/v1/agent-versions/version-1/complete",
    ]
    uploads = [request for request in service.requests if request[1] == "/direct-upload"]
    assert uploads[0] == uploads[1]
    assert service.direct_upload_stored == {"Bearer source-upload"}


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"upload_url": "file:///tmp/upload"}, "invalid response"),
        ({"upload_prefix": "!"}, "invalid response"),
        ({"upload_url": "http://127.0.0.1:9/upload"}, "application upload is incomplete"),
    ],
)
def test_deploy_rejects_invalid_or_unreachable_direct_transfers(  # noqa: PLR0913, PLR0917 - fixtures plus parameters
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    changes: dict[str, str],
    message: str,
) -> None:
    """Malformed and unreachable transfer descriptors fail without a traceback."""
    app = write_app(tmp_path / "app")
    service.registration["source_upload"].update(changes)

    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert message in capsys.readouterr().err


def test_deploy_reports_the_service_resolved_model(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The printed selection comes from the prepared version, including its default."""
    app = write_app(tmp_path / "app")
    service.version_model = "gpt-6-astra"
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    assert main(["deploy", str(app), "--as", "mcp"]) == 0
    assert "model: gpt-6-astra" in capsys.readouterr().out


def test_deploy_explains_an_unsupported_model(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rejected selection directs the author to the declaration, not a fallback."""
    app = write_app(tmp_path / "app")
    service.version_statuses = ["failed"]
    service.version_error = "unsupported_model"
    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert "agent.model in agent.yaml" in capsys.readouterr().err
    assert not any(path == "/v1/deployments" for _, path, _, _ in service.requests)


def test_deploy_reports_a_failed_deployment_build(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed version surfaces the public build_failed error."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    service.version_statuses = ["failed"]

    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert "build_failed" in capsys.readouterr().err


def test_deploy_reports_how_to_resolve_an_insufficient_balance(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pre-dispatch wallet denial gives the exact recovery commands."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    service.version_statuses = ["failed"]
    service.version_error = "insufficient_balance"

    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert (
        "Run recurse billing top-up 5 or recurse billing redeem CODE, then retry deployment"
        in capsys.readouterr().err
    )


def test_run_reports_how_to_resolve_an_insufficient_balance(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A pre-dispatch wallet denial names the direct run being retried."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    service.version_statuses = ["failed"]
    service.version_error = "insufficient_balance"

    assert main(["run", str(app)]) == 2
    message = capsys.readouterr().err
    assert (
        "Run recurse billing top-up 5 or recurse billing redeem CODE, then retry the run" in message
    )
    assert "retry deployment" not in message


def test_deploy_gives_up_when_the_build_never_finishes(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Polling stops after the attempt limit with a clear message."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    monkeypatch.setattr("_recurse_cli._POLL_ATTEMPTS", 2)
    service.version_statuses = ["processing"] * 3

    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert "did not finish" in capsys.readouterr().err


def test_deploy_reports_authoring_errors_without_a_traceback(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Authoring failures print one error line and exit nonzero."""
    empty = tmp_path / "empty"
    empty.mkdir()

    assert main(["deploy", str(empty), "--as", "mcp"]) == 2
    assert "agent.yaml was not found" in capsys.readouterr().err


def test_commands_require_login_first(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a stored credential no request is sent and login is suggested."""
    app = write_app(tmp_path / "app")

    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert "recurse login" in capsys.readouterr().err
    assert service.requests == []


def test_billing_balance_prints_total_and_spendable_microdollars(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Balance preserves sub-cent usage instead of rounding money away."""
    assert main(["billing", "balance"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "Balance: $5.000000",
        "Available: $4.921276",
    ]
    assert service.requests[-1][0:3] == ("GET", "/v1/billing", None)


@pytest.mark.parametrize("amount_case", [("5", 500), ("10", 1000), ("20", 2000), ("12.34", 1234)])
def test_billing_top_up_opens_checkout_for_the_exact_amount(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    amount_case: tuple[str, int],
) -> None:
    """Preset and custom top-ups send exact integer cents to hosted Checkout."""
    amount, amount_cents = amount_case
    opened: list[str] = []

    def open_recorded(url: str) -> bool:
        """Record the opened URL."""
        opened.append(url)
        return True

    monkeypatch.setattr("webbrowser.open", open_recorded)

    assert main(["billing", "top-up", amount]) == 0

    assert opened == ["https://billing.recurse.run/checkout/session-1"]
    output = capsys.readouterr().out
    assert output == "Opened Recurse billing in your browser.\n"
    assert "checkout/session-1" not in output
    body = service.requests[-1][2]
    assert isinstance(body, dict)
    assert set(body) == {"amount_cents", "idempotency_key"}
    assert body["amount_cents"] == amount_cents
    assert str(UUID(body["idempotency_key"])) == body["idempotency_key"]


@pytest.mark.parametrize(
    "amount",
    [
        "0",
        "4.99",
        "500.01",
        "5.001",
        "NaN",
        "sNaN",
        "-sNaN",
        "Infinity",
        "1e999999",
        "4.999999999999999999999999999999",
        "5.000000000000000000000000000001",
        "12.339999999999999999999999999999",
        "not-money",
    ],
)
def test_billing_top_up_rejects_invalid_amounts_before_login(
    capsys: pytest.CaptureFixture[str],
    amount: str,
) -> None:
    """Top-up accepts only finite cent amounts from $5 through $500."""
    arguments = ["billing", "top-up", amount]
    if amount.startswith("-"):
        arguments.insert(-1, "--")
    with pytest.raises(SystemExit):
        main(arguments)
    assert "amount must be from $5.00 to $500.00" in capsys.readouterr().err


def test_billing_redeem_sends_the_opaque_code_and_prints_the_new_balance(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A code is sent only in the redeem body and the resulting wallet is shown."""
    code = "rc_" + "A" * 24

    assert main(["billing", "redeem", code]) == 0

    assert service.requests[-1][0:3] == (
        "POST",
        "/v1/billing/credit-codes/redeem",
        {"code": code},
    )
    assert capsys.readouterr().out.splitlines() == [
        "Credit applied.",
        "Balance: $10.000000",
        "Available: $9.921276",
    ]


def test_billing_redeem_rejects_a_malformed_code_without_logging_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed code guesses stop locally and are not echoed back."""
    with pytest.raises(SystemExit):
        main(["billing", "redeem", "secret-guess"])

    error = capsys.readouterr().err
    assert "invalid Recurse credit code" in error
    assert "secret-guess" not in error


@pytest.mark.parametrize("browser_result", [False, webbrowser.Error("no browser")])
def test_billing_prints_the_hosted_url_only_when_the_browser_does_not_open(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    browser_result: bool | webbrowser.Error,
) -> None:
    """A headless user receives the hosted URL while normal terminals stay concise."""

    def unavailable_browser(url: str) -> bool:
        """Return or raise the selected browser failure."""
        del url
        if isinstance(browser_result, Exception):
            raise browser_result
        return browser_result

    monkeypatch.setattr("webbrowser.open", unavailable_browser)

    assert main(["billing", "top-up", "5"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Your browser did not open. Continue to Recurse billing here:",
        "https://billing.recurse.run/checkout/session-1",
    ]


def test_billing_no_open_prints_the_hosted_url_without_starting_a_browser(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Headless users can request the hosted URL without trusting browser detection."""

    def unexpected_browser(url: str) -> bool:
        """Fail if the no-open path attempts a browser launch."""
        raise AssertionError(url)

    monkeypatch.setattr("webbrowser.open", unexpected_browser)

    assert main(["billing", "top-up", "5", "--no-open"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Continue to Recurse billing here:",
        "https://billing.recurse.run/checkout/session-1",
    ]


def test_service_errors_surface_their_public_detail(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The service's public detail message is printed verbatim."""
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    service.fail_detail["/v1/billing/checkout"] = (409, "wallet checkout already exists")

    assert main(["billing", "top-up", "5"]) == 2
    assert "wallet checkout already exists" in capsys.readouterr().err


def test_unreachable_service_is_reported_clearly(
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A connection failure becomes a could-not-be-reached message."""
    unreachable_url = "http://127.0.0.1:9"
    monkeypatch.setenv("RECURSE_API_URL", unreachable_url)
    logged_in[_credential_key(unreachable_url)] = "device-1"

    assert main(["billing", "balance"]) == 2
    assert "could not be reached" in capsys.readouterr().err


def test_non_json_error_bodies_fall_back_to_the_status_reason(
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-JSON error body falls back to the HTTP reason phrase."""

    class Handler(BaseHTTPRequestHandler):
        """Answer every request with a bad gateway and a junk body."""

        def log_message(self, *args: object) -> None:
            """Keep the test output free of request logging."""
            del args

        def do_POST(self) -> None:
            """Reply 502 with a non-JSON body."""
            self.send_response(502)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"!!")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    malformed_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("RECURSE_API_URL", malformed_url)
    logged_in[_credential_key(malformed_url)] = "device-1"
    try:
        assert main(["billing", "top-up", "5"]) == 2
        assert "Bad Gateway" in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()


def test_misconfigured_api_url_is_rejected(
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A non-HTTP RECURSE_API_URL is refused before any request."""
    monkeypatch.setenv("RECURSE_API_URL", "ftp://example.invalid")

    assert main(["billing", "balance"]) == 2
    assert "RECURSE_API_URL" in capsys.readouterr().err


def test_stray_browser_requests_do_not_finish_the_login(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray asset request is answered without ending the login wait."""

    def open_with_stray_request(url: str) -> bool:
        """Hit a non-callback path first, then complete the login."""
        parsed = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        base = parsed["redirect_to"].rsplit("/", 1)[0]
        with urllib.request.urlopen(  # noqa: S310 - test-generated loopback URL
            f"{base}/favicon.ico"
        ) as response:
            assert b"not completed" in response.read()
        return _browser_completing_login()(url)

    monkeypatch.setattr("webbrowser.open", open_with_stray_request)
    assert main(["login"]) == 0
    assert keychain[_credential_key(service.url)] == "device-1"


def test_query_on_a_non_callback_path_does_not_finish_login() -> None:
    """A query string cannot end the wait unless it targets the callback path."""
    server = _LoginServer("expected-state")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/other?" + urllib.parse.urlencode(
            {"code": "code-1", "state": "expected-state"}
        )
        with urllib.request.urlopen(url) as response:
            assert b"not completed" in response.read()
        assert not server.done.is_set()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_help_shows_the_public_commands(capsys: pytest.CaptureFixture[str]) -> None:
    """--help exits 0 and lists the public workflows."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for command in ["login", "logout", "secret", "run", "deploy", "mcp", "billing"]:
        assert command in out


def test_mcp_help_shows_the_serve_command_and_deployment_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nested help makes the complete local MCP command discoverable."""
    with pytest.raises(SystemExit) as command_exit:
        main(["mcp", "--help"])
    assert command_exit.value.code == 0
    assert "serve" in capsys.readouterr().out

    with pytest.raises(SystemExit) as serve_exit:
        main(["mcp", "serve", "--help"])
    assert serve_exit.value.code == 0
    assert "deployment_id" in capsys.readouterr().out


def test_login_callback_page_is_served_to_the_browser(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The browser receives a branded close-this-window card on success."""
    pages: list[bytes] = []

    def open_and_read(url: str) -> bool:
        """Complete the callback and keep the served page."""
        parsed = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        callback = (
            parsed["redirect_to"]
            + "?"
            + urllib.parse.urlencode({"code": "code-1", "state": parsed["state"]})
        )
        with urllib.request.urlopen(callback) as response:  # noqa: S310 - loopback URL
            pages.append(response.read())
        return True

    monkeypatch.setattr("webbrowser.open", open_and_read)
    assert main(["login"]) == 0
    page = pages[0].decode()
    assert "<title>Authorization received · Recurse</title>" in page
    assert 'class="result-card result-card--success"' in page
    assert '<svg class="brand-mark" aria-hidden="true" viewBox="0 0 300 300"' in page
    assert ".brand-mark { border-radius:" not in page
    assert "#5C3DF5" in page
    assert '<span class="brand-mark" aria-hidden="true">R</span>' not in page
    assert "Return to your terminal" in page
    assert "signed in" not in page


def test_failed_login_callback_page_is_designed_for_retry() -> None:
    """A rejected callback renders a branded failure card with the retry command."""
    server = _LoginServer("expected-state")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        callback = f"http://127.0.0.1:{server.server_port}/callback?" + urllib.parse.urlencode(
            {"code": "code-1", "state": "wrong-state"}
        )
        with urllib.request.urlopen(callback) as response:
            page = response.read().decode()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert "<title>Login not completed · Recurse</title>" in page
    assert 'class="result-card result-card--failure"' in page
    assert '<svg class="brand-mark" aria-hidden="true" viewBox="0 0 300 300"' in page
    assert ".brand-mark { border-radius:" not in page
    assert "#5C3DF5" in page
    assert '<span class="brand-mark" aria-hidden="true">R</span>' not in page
    assert "<code>recurse login</code>" in page


def test_keychain_failures_are_reported_without_a_traceback(
    service: FakeService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A keychain read failure prints one keychain error line."""

    def broken(system: str, name: str) -> str:
        """Fail like a locked keychain backend."""
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("keyring.get_password", broken)
    assert main(["billing", "balance"]) == 2
    assert "keychain" in capsys.readouterr().err


def test_keychain_write_failures_are_reported_without_a_traceback(
    service: FakeService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A keychain write failure prints one keychain error line."""
    monkeypatch.setattr("webbrowser.open", _browser_completing_login())

    def broken(system: str, name: str, value: str) -> None:
        """Fail like a locked keychain backend."""
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("keyring.set_password", broken)
    monkeypatch.setattr("keyring.get_password", lambda system, name: None)
    assert main(["login"]) == 2
    assert "keychain" in capsys.readouterr().err


def test_occupied_callback_port_is_reported_without_a_traceback(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An occupied callback port becomes a concise login error."""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    try:
        monkeypatch.setattr("_recurse_cli._CALLBACK_PORT", blocker.getsockname()[1])
        assert main(["login"]) == 2
        assert "login callback" in capsys.readouterr().err
    finally:
        blocker.close()


def test_callbacks_are_accepted_only_on_the_callback_path(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid code on the wrong path never completes the login."""

    def open_with_wrong_path(url: str) -> bool:
        """Deliver a valid code and state to a non-callback path."""
        parsed = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        base = parsed["redirect_to"].rsplit("/", 1)[0]
        query = urllib.parse.urlencode({"code": "code-1", "state": parsed["state"]})
        with urllib.request.urlopen(  # noqa: S310 - test-generated loopback URL
            f"{base}/other?{query}"
        ) as response:
            assert b"not completed" in response.read()
        return True

    monkeypatch.setattr("webbrowser.open", open_with_wrong_path)
    monkeypatch.setattr("_recurse_cli._LOGIN_WAIT_SECONDS", 1)
    assert main(["login"]) == 2
    assert keychain == {}


def test_malformed_service_success_bodies_are_service_errors(
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-JSON and non-object success bodies become service errors."""

    class Handler(BaseHTTPRequestHandler):
        """Answer 200 with a configurable malformed body."""

        body = b"not-json"

        def log_message(self, *args: object) -> None:
            """Keep the test output free of request logging."""
            del args

        def do_POST(self) -> None:
            """Reply 200 with the configured body."""
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.body)))
            self.end_headers()
            self.wfile.write(self.body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    malformed_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("RECURSE_API_URL", malformed_url)
    logged_in[_credential_key(malformed_url)] = "device-1"
    try:
        assert main(["billing", "top-up", "5"]) == 2
        assert "invalid response" in capsys.readouterr().err
        Handler.body = b'["not", "an", "object"]'
        assert main(["billing", "top-up", "5"]) == 2
        assert "invalid response" in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()


def test_login_rejects_a_token_response_without_a_device_credential(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A token grant missing device_credential is a concise error and stores nothing."""
    monkeypatch.setattr("webbrowser.open", _browser_completing_login())
    service.malformed["/v1/auth/token"] = {"access_token": "access-1", "token_type": "bearer"}

    assert main(["login"]) == 2

    assert keychain == {}
    assert "missing device_credential" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        ("/v1/agent-versions", {"agent_id": "agent-1", "version": 1}, "version_id"),
        (
            "/v1/agent-versions",
            {
                "agent_id": "agent-1",
                "version_id": "version-1",
                "version": 1,
                "source_upload": None,
            },
            "invalid response",
        ),
        ("/v1/agent-versions/version-1", {"version_id": "version-1", "status": 7}, "status"),
        (
            "/v1/agent-versions/version-1",
            {"version_id": "version-1", "status": "failed"},
            "error",
        ),
        ("/v1/deployments", {"deployment_id": "mcp_1234"}, "endpoint"),
    ],
)
def test_deploy_reports_malformed_success_responses(  # noqa: PLR0913, PLR0917 - fixtures plus parameters
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    path: str,
    body: dict[str, object],
    field: str,
) -> None:
    """Each missing or unusable required deploy field is a concise error, no traceback."""
    app = write_app(tmp_path / "app")
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)
    service.malformed[path] = body
    assert main(["deploy", str(app), "--as", "mcp"]) == 2
    assert field in capsys.readouterr().err


def test_deploy_reads_the_manifest_once(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registration metadata comes from the same manifest used for the bundle."""
    app = write_app(tmp_path / "app")
    original = Path.read_text
    reads = 0

    def counted(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        """Count manifest reads while preserving normal path behavior."""
        nonlocal reads
        if path == app / "agent.yaml":
            reads += 1
        return original(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", counted)
    monkeypatch.setattr("_recurse_cli._POLL_SECONDS", 0)

    assert main(["deploy", str(app), "--as", "mcp"]) == 0
    assert reads == 1


def test_billing_reports_malformed_success_responses(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hosted-page response without a usable URL is a concise error, no browser open."""
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    service.malformed["/v1/billing/checkout"] = {"checkout_url": ""}
    service.malformed["/v1/billing"] = {"balance_microusd": "5"}
    service.malformed["/v1/billing/credit-codes/redeem"] = {
        "balance_microusd": 5_000_000,
        "available_balance_microusd": 6_000_000,
    }

    assert main(["billing", "top-up", "5"]) == 2
    assert "missing checkout_url" in capsys.readouterr().err
    assert main(["billing", "balance"]) == 2
    assert "invalid wallet response" in capsys.readouterr().err
    assert main(["billing", "redeem", "rc_" + "A" * 24]) == 2
    output = capsys.readouterr()
    assert "invalid wallet response" in output.err
    assert "Credit applied" not in output.out


def test_login_reports_a_malformed_account_response(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An account response without a display name is a concise error after login."""
    monkeypatch.setattr("webbrowser.open", _browser_completing_login())
    service.malformed["/v1/account"] = {"account_id": "a" * 32, "display_name": 7}

    assert main(["login"]) == 2
    assert keychain == {}
    assert "missing display_name" in capsys.readouterr().err


def test_device_credential_is_exchanged_without_rotation(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A stored device credential yields a short-lived access token without mutation."""
    assert cli._access_token() == "access-1"

    assert service.requests == [
        (
            "POST",
            "/v1/auth/token",
            {"grant_type": "device_credential", "device_credential": "device-1"},
            None,
        )
    ]
    assert logged_in[_credential_key(service.url)] == "device-1"
    cached = json.loads(logged_in[("recurse-cli", f"access-token:{service.url}")])
    assert cached["access_token"] == "access-1"  # noqa: S105 - fake bearer


def test_parallel_access_token_requests_share_one_exchange(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Twelve concurrent callers must not consume twelve provider sign-ins."""
    barrier = threading.Barrier(12)

    def obtain_token(_: int) -> str:
        """Start all callers together, using the real token HTTP endpoint."""
        barrier.wait(timeout=10)
        return cli._access_token()

    with ThreadPoolExecutor(max_workers=12) as workers:
        assert list(workers.map(obtain_token, range(12))) == ["access-1"] * 12
    assert service.device_grants == ["device-1"]


def test_parallel_rejected_tokens_share_one_refresh(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late failures of an old bearer reuse, rather than discard, its replacement."""
    service.token_responses = ["access-old", "access-new"]
    old = cli._access_token()

    def reject_old(*args: Any, **kwargs: Any) -> tuple[int, bytes]:
        """Reject only the old bearer at the MCP boundary."""
        if args[3] == old:
            return 401, b'{"detail":"expired"}'
        return 200, b'{"jsonrpc":"2.0","id":1,"result":{}}'

    monkeypatch.setattr(cli, "_mcp_request", reject_old)

    def invoke(_: int) -> str:
        """Exercise the bridge's existing one-retry request path."""
        return cli._remote_mcp_request("mcp_test", 1, "tools/list", {}, old)[1]

    with ThreadPoolExecutor(max_workers=12) as workers:
        assert list(workers.map(invoke, range(12))) == ["access-new"] * 12
    assert service.device_grants == ["device-1", "device-1"]


def test_logout_revokes_then_removes_the_device_credential(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Logout removes the local credential only after server-side revocation succeeds."""
    assert main(["logout"]) == 0

    assert logged_in == {}
    assert service.requests[-1] == (
        "POST",
        "/v1/auth/logout",
        {"device_credential": "device-1"},
        "Bearer access-1",
    )
    output = capsys.readouterr().out
    assert f"Logged out of Recurse at {service.url}." in output
    assert "saved CLI login was removed from your keychain" in output


def test_logout_preserves_the_device_credential_when_revocation_fails(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rejected revocation is reported and leaves the usable local credential intact."""
    service.fail_detail["/v1/auth/logout"] = (503, "logout is temporarily unavailable")

    assert main(["logout"]) == 2

    assert logged_in == {_credential_key(service.url): "device-1"}
    assert "Service Unavailable (HTTP 503)" in capsys.readouterr().err


@pytest.mark.parametrize(
    "transport_error",
    [
        urllib.error.URLError("network is offline"),
        http.client.BadStatusLine("not an HTTP response"),
    ],
    ids=["network-error", "malformed-response"],
)
def test_logout_preserves_the_device_credential_after_a_transport_failure(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    transport_error: Exception,
) -> None:
    """A revocation transport failure is reported without deleting the credential."""
    real_urlopen = urllib.request.urlopen

    def malformed_logout(request: Any, *, timeout: int) -> Any:
        """Return real responses except for the failed logout transport."""
        if request.full_url.endswith("/v1/auth/logout"):
            raise transport_error
        return real_urlopen(request, timeout=timeout)

    monkeypatch.setattr("urllib.request.urlopen", malformed_logout)

    assert main(["logout"]) == 2
    assert logged_in == {_credential_key(service.url): "device-1"}
    assert "could not be reached" in capsys.readouterr().err


def test_logout_reports_keychain_deletion_failure_after_revocation(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A keychain deletion failure is visible after successful server revocation."""

    def fail_delete(system: str, name: str) -> None:
        """Simulate a keychain that cannot delete an entry."""
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("keyring.delete_password", fail_delete)

    assert main(["logout"]) == 2
    assert logged_in == {_credential_key(service.url): "device-1"}
    assert "keychain" in capsys.readouterr().err


def _bridge_frames(*messages: dict[str, Any]) -> tuple[io.BytesIO, io.BytesIO]:
    """Run bridge input and output streams containing JSON-RPC messages."""
    raw = b"\n".join(json.dumps(message).encode() for message in messages) + b"\n"
    return io.BytesIO(raw), io.BytesIO()


def _initialize_frame(request_id: object, protocol_version: str = "2025-11-25") -> dict[str, Any]:
    """Return the ordinary MCP initialization frame used as test setup.

    Args:
        request_id: Host request identity.
        protocol_version: MCP protocol version requested by the host.

    Returns:
        A minimal initialization request.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {"protocolVersion": protocol_version},
    }


class _StreamingInput:
    """Yield binary MCP frames as a host sends them."""

    def __init__(self) -> None:
        """Create an empty blocking frame queue."""
        self.frames: queue.Queue[bytes | None] = queue.Queue()

    def __iter__(self) -> _StreamingInput:
        """Return the stream iterator."""
        return self

    def __next__(self) -> bytes:
        """Block until the host sends a frame or closes the stream."""
        frame = self.frames.get(timeout=2)
        if frame is None:
            raise StopIteration
        return frame

    def send(self, message: dict[str, Any]) -> None:
        """Send one newline-delimited host frame."""
        self.frames.put(json.dumps(message).encode() + b"\n")

    def close(self) -> None:
        """End the host input stream."""
        self.frames.put(None)


def _wait_for_mcp_method(service: FakeService, method: str, timeout: float = 1) -> bool:
    """Wait until the loopback service observes one remote MCP method."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(headers.get("Mcp-Method") == method for headers in service.mcp_headers):
            return True
        time.sleep(0.005)
    return False


def _wait_for_host_response(
    output_stream: io.BytesIO, request_id: object, timeout: float = 1
) -> dict[str, Any] | None:
    """Wait for one exact JSON-RPC response written by the bridge."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in output_stream.getvalue().splitlines():
            response = json.loads(line)
            if response.get("id") == request_id:
                return cast(dict[str, Any], response)
        time.sleep(0.005)
    return None


def _remote_reply(request_id: object, result: dict[str, Any]) -> dict[str, Any]:
    """Wrap one complete scripted remote JSON-RPC result."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _remote_discovery_reply(request_id: object) -> dict[str, Any]:
    """Return the complete task-capable discovery response used by bridge tests."""
    return _remote_reply(
        request_id,
        {
            "supportedVersions": ["2026-07-28"],
            "capabilities": {
                "tools": {"listChanged": False},
                "extensions": {"io.modelcontextprotocol/tasks": {}},
            },
            "_meta": {
                "io.modelcontextprotocol/serverInfo": {
                    "name": "recurse",
                    "version": "0.1.0",
                }
            },
        },
    )


def test_mcp_bridge_translates_standard_initialize_and_consumes_initialized(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A standard handshake becomes remote discovery while its notification stays local."""
    input_stream, output_stream = _bridge_frames(
        {
            "jsonrpc": "2.0",
            "id": "host-init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test-host", "version": "1"},
            },
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert [json.loads(line) for line in output_stream.getvalue().splitlines()] == [
        {
            "jsonrpc": "2.0",
            "id": "host-init",
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {},
                },
                "serverInfo": {"name": "recurse", "version": "0.1.0"},
            },
        }
    ]
    assert len(service.mcp_bodies) == 1
    remote = json.loads(service.mcp_bodies[0])
    assert remote == {
        "jsonrpc": "2.0",
        "id": "host-init",
        "method": "server/discover",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {
                    "name": "recurse-sdk",
                    "version": "0.1.8",
                },
                "io.modelcontextprotocol/clientCapabilities": {
                    "extensions": {"io.modelcontextprotocol/tasks": {}}
                },
            }
        },
    }
    assert service.device_grants == ["device-1"]
    headers = {name.lower(): value for name, value in service.mcp_headers[0].items()}
    assert headers["authorization"] == "Bearer access-1"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert headers["mcp-protocol-version"] == "2026-07-28"
    assert headers["mcp-method"] == "server/discover"
    assert "mcp-name" not in headers


def test_mcp_bridge_reports_missing_login_in_the_initialize_response(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
) -> None:
    """An MCP host receives login guidance instead of an unexplained startup EOF."""
    input_stream, output_stream = _bridge_frames(_initialize_frame("host-init"))

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert keychain == {}
    assert service.requests == []
    assert json.loads(output_stream.getvalue()) == {
        "jsonrpc": "2.0",
        "id": "host-init",
        "error": {
            "code": -32000,
            "message": "you are not logged in; run: recurse login",
        },
    }


def test_mcp_bridge_reports_a_rejected_saved_login_in_the_initialize_response(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A revoked saved login produces actionable host output instead of startup EOF."""
    service.fail_detail["/v1/auth/token"] = (401, "login was rejected")
    input_stream, output_stream = _bridge_frames(_initialize_frame("host-init"))

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert logged_in == {_credential_key(service.url): "device-1"}
    assert service.mcp_bodies == []
    assert json.loads(output_stream.getvalue()) == {
        "jsonrpc": "2.0",
        "id": "host-init",
        "error": {
            "code": -32000,
            "message": (
                "login was rejected. If your saved login was revoked or expired, run: recurse login"
            ),
        },
    }


def test_mcp_bridge_adds_the_remote_list_envelope_and_preserves_the_host_id(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Tool listing retains host pagination and id while adding remote task capability."""
    service.mcp_responses = [
        {
            "jsonrpc": "2.0",
            "id": "host-init",
            "result": {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {"listChanged": False}},
                "_meta": {
                    "io.modelcontextprotocol/serverInfo": {
                        "name": "recurse",
                        "version": "0.1.0",
                    }
                },
            },
        },
        {
            "jsonrpc": "2.0",
            "id": "host-list",
            "result": {
                "tools": [
                    {
                        "name": "tune",
                        "description": "Tune a model.",
                        "inputSchema": {"type": "object"},
                    }
                ]
            },
        },
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame("host-init"),
        {
            "jsonrpc": "2.0",
            "id": "host-list",
            "method": "tools/list",
            "params": {
                "cursor": "page-2",
                "_meta": {
                    "recurse.run/resources": {
                        "cpu_limit": 2.0,
                        "memory_limit_mib": 2048,
                    }
                },
            },
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[1] == {
        "jsonrpc": "2.0",
        "id": "host-list",
        "result": {
            "tools": [
                {
                    "name": "tune",
                    "description": "Tune a model.",
                    "inputSchema": {"type": "object"},
                }
            ]
        },
    }
    remote = json.loads(service.mcp_bodies[1])
    assert remote["id"] == "host-list"
    assert remote["method"] == "tools/list"
    assert remote["params"]["cursor"] == "page-2"
    assert remote["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {
        "extensions": {"io.modelcontextprotocol/tasks": {}}
    }
    assert "recurse.run/resources" not in remote["params"]["_meta"]
    headers = {name.lower(): value for name, value in service.mcp_headers[1].items()}
    assert headers["mcp-method"] == "tools/list"
    assert "mcp-name" not in headers


def test_mcp_bridge_polls_an_async_call_and_returns_the_embedded_result(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A working task is polled at server intervals and becomes a normal tool result."""
    task_id = "task_" + "a" * 32
    call_result: dict[str, Any] = {
        "content": [{"type": "text", "text": "done"}],
        "structuredContent": {"status": "succeeded"},
        "isError": False,
    }
    service.mcp_responses = [
        _remote_discovery_reply("host-init"),
        _remote_reply(
            "host-call",
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 7,
            },
        ),
        _remote_reply(
            "host-call",
            {
                "resultType": "complete",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 11,
            },
        ),
        _remote_reply(
            "host-call",
            {
                "resultType": "complete",
                "taskId": task_id,
                "status": "completed",
                "result": call_result,
            },
        ),
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame("host-init"),
        {
            "jsonrpc": "2.0",
            "id": "host-call",
            "method": "tools/call",
            "params": {
                "name": "tune",
                "arguments": {"trials": 2},
                "_meta": {
                    "recurse.run/resources": {
                        "cpu_limit": 2.0,
                        "memory_limit_mib": 2048,
                    },
                    "host-private": "must-not-cross",
                },
            },
        },
    )

    started = time.monotonic()
    cli._serve_mcp("mcp_1234", input_stream, output_stream)
    elapsed = time.monotonic() - started

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[-1] == {
        "jsonrpc": "2.0",
        "id": "host-call",
        "result": {
            **call_result,
            "content": [*call_result["content"], {"type": "text", "text": f"Task: {task_id}"}],
        },
    }
    assert 0.018 <= elapsed < 1
    assert [headers["Mcp-Method"] for headers in service.mcp_headers] == [
        "server/discover",
        "tools/call",
        "tasks/get",
        "tasks/get",
    ]
    assert service.mcp_headers[1]["Mcp-Name"] == "tune"
    assert all("Mcp-Name" not in headers for headers in service.mcp_headers[2:])
    remote_call = json.loads(service.mcp_bodies[1])
    assert remote_call["id"] == "host-call"
    assert remote_call["params"]["name"] == "tune"
    assert remote_call["params"]["arguments"] == {"trials": 2}
    assert remote_call["params"]["_meta"]["recurse.run/resources"] == {
        "cpu_limit": 2.0,
        "memory_limit_mib": 2048,
    }
    assert "host-private" not in remote_call["params"]["_meta"]
    for body in service.mcp_bodies[1:]:
        remote = json.loads(body)
        assert remote["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"] == {
            "extensions": {"io.modelcontextprotocol/tasks": {}}
        }
    for body in service.mcp_bodies[2:]:
        assert json.loads(body)["params"]["taskId"] == task_id


def test_mcp_bridge_reads_and_verifies_an_artifact_resource(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """An owner can read exact verified artifact bytes through the MCP link."""
    run_id = "77777777-7777-4777-8777-777777777777"
    output_id = "99999999-9999-4999-8999-999999999999"
    uri = f"recurse://artifact/{run_id}/{output_id}"
    service.mcp_responses = [_remote_discovery_reply(1)]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": "application/octet-stream",
                    "blob": base64.b64encode(service.artifact_bytes).decode(),
                }
            ]
        },
    }
    grant = next(request for request in service.requests if "/artifacts/" in request[1])
    assert grant == (
        "GET",
        f"/v1/runs/{run_id}/artifacts/{output_id}",
        None,
        "Bearer access-1",
    )
    assert service.device_grants == ["device-1"]
    assert service.artifact_download_authorization == "Bearer artifact-token"
    assert service.artifact_download_user_agent == "recurse-sdk/0.1.8"


@pytest.mark.parametrize(
    ("method", "params", "result"),
    [
        pytest.param(
            "resources/list",
            {"cursor": "ignored"},
            {"resources": []},
            id="resources",
        ),
        pytest.param(
            "resources/templates/list",
            {},
            {"resourceTemplates": []},
            id="resource-templates",
        ),
    ],
)
def test_mcp_bridge_lists_no_global_artifact_resources(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    method: str,
    params: dict[str, object],
    result: dict[str, list[object]],
) -> None:
    """Call-scoped artifact links stay absent from global resource discovery."""
    service.mcp_responses = [_remote_discovery_reply(1)]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": method,
            "params": params,
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": result,
    }
    assert len(service.mcp_bodies) == 1


def test_mcp_bridge_rejects_artifact_bytes_that_do_not_match_the_grant(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Corrupt Storage bytes never satisfy an artifact resource read."""
    service.artifact_sha256 = "0" * 64
    service.mcp_responses = [_remote_discovery_reply(1)]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/read",
            "params": {
                "uri": (
                    "recurse://artifact/77777777-7777-4777-8777-777777777777/"
                    "99999999-9999-4999-8999-999999999999"
                )
            },
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32002, "message": "artifact verification failed"},
    }


@pytest.mark.parametrize(
    "uri",
    [
        None,
        "https://artifact/77777777-7777-4777-8777-777777777777/"
        "99999999-9999-4999-8999-999999999999",
        "recurse://other/77777777-7777-4777-8777-777777777777/99999999-9999-4999-8999-999999999999",
        "recurse://artifact/not-a-uuid/99999999-9999-4999-8999-999999999999",
        "recurse://artifact/AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA/"
        "99999999-9999-4999-8999-999999999999",
        "recurse://artifact/77777777-7777-4777-8777-777777777777/"
        "99999999-9999-4999-8999-999999999999;version=1",
        "recurse://artifact/77777777-7777-4777-8777-777777777777/"
        "99999999-9999-4999-8999-999999999999?download=1",
        "recurse://artifact/77777777-7777-4777-8777-777777777777/"
        "99999999-9999-4999-8999-999999999999#artifact",
        "recurse://artifact/77777777-7777-4777-8777-777777777777",
    ],
)
def test_mcp_bridge_rejects_noncanonical_artifact_uris(uri: object) -> None:
    """Only exact Recurse artifact identities can trigger a download."""
    with pytest.raises(cli._CliError, match="invalid artifact resource URI"):
        cli._artifact_ids(uri)


@pytest.mark.parametrize(
    "change",
    [
        {"download_url": "file:///private/artifact"},
        {"size_bytes": "12"},
        {"size_bytes": True},
        {"size_bytes": -1},
        {"size_bytes": 64_000_001},
        {"sha256": "0" * 63},
        {"sha256": "g" * 64},
    ],
)
def test_mcp_bridge_rejects_malformed_artifact_grants(change: dict[str, Any]) -> None:
    """Malformed service grants cannot direct or widen a local download."""
    grant: dict[str, Any] = {
        "download_url": "https://storage.example/artifact",
        "download_token": "download-token",
        "size_bytes": 12,
        "sha256": "0" * 64,
    }
    grant.update(change)

    with pytest.raises(cli.ServiceError, match="invalid artifact grant"):
        cli._download_artifact(grant)


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            "https://storage.example/artifact",
            503,
            "unavailable",
            Message(),
            io.BytesIO(b"unavailable"),
        ),
        urllib.error.URLError("offline"),
    ],
)
def test_mcp_bridge_reports_artifact_download_failures(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Storage transport failures become one safe public error."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    grant = {
        "download_url": "https://storage.example/artifact",
        "download_token": "download-token",
        "size_bytes": 12,
        "sha256": "0" * 64,
    }

    with pytest.raises(cli.ServiceError, match="artifact download failed"):
        cli._download_artifact(grant)


def test_mcp_bridge_rejects_an_artifact_size_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short Storage response cannot satisfy its committed artifact size."""
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b"short"),
    )

    with pytest.raises(cli.ServiceError, match="artifact verification failed"):
        cli._download_artifact(
            {
                "download_url": "https://storage.example/artifact",
                "download_token": "download-token",
                "size_bytes": 6,
                "sha256": hashlib.sha256(b"short").hexdigest(),
            }
        )


@pytest.mark.parametrize("message", [{"id": 1}, {"id": 1, "params": []}])
def test_mcp_bridge_rejects_malformed_resource_reads(message: dict[str, Any]) -> None:
    """Resource reads require a request id and object parameters."""
    with pytest.raises(cli._CliError, match="malformed JSON-RPC frame"):
        cli._read_mcp_resource(message)


def test_mcp_bridge_forwards_host_cancellation_to_the_remote_task(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling one host tool call cancels its exact admitted remote task."""
    task_id = "task_" + "7" * 32
    service.mcp_live_task_id = task_id
    service.mcp_responses = [
        _remote_discovery_reply("init"),
        _remote_reply(
            "call",
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 10,
            },
        ),
    ]
    monkeypatch.setattr(cli, "_MCP_TASK_TIMEOUT_SECONDS", 0.3)
    input_stream = _StreamingInput()
    output_stream = io.BytesIO()
    errors: list[BaseException] = []

    def serve() -> None:
        """Run the bridge and retain any process-level failure."""
        try:
            cli._serve_mcp("mcp_1234", input_stream, output_stream)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    input_stream.send(_initialize_frame("init"))
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        }
    )
    assert _wait_for_mcp_method(service, "tools/call")
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "call", "reason": "user stopped the call"},
        }
    )
    forwarded = _wait_for_mcp_method(service, "tasks/cancel", timeout=0.2)
    input_stream.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []
    assert forwarded
    methods = [headers["Mcp-Method"] for headers in service.mcp_headers]
    assert methods.count("tools/call") == 1
    assert methods.count("tasks/cancel") == 1
    cancelled = json.loads(
        next(
            body
            for body, headers in zip(service.mcp_bodies, service.mcp_headers, strict=True)
            if headers["Mcp-Method"] == "tasks/cancel"
        )
    )
    assert cancelled["params"]["taskId"] == task_id
    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert [response["id"] for response in responses] == ["init"]


def test_mcp_bridge_answers_a_failed_call_while_the_host_stream_stays_open(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A worker failure answers its request without waiting for host disconnect."""
    service.mcp_responses = [
        _remote_discovery_reply("init"),
        _remote_reply("call", {"status": "working", "pollIntervalMs": 0}),
        _remote_reply("list", {"tools": []}),
    ]
    input_stream = _StreamingInput()
    output_stream = io.BytesIO()
    errors: list[BaseException] = []

    def serve() -> None:
        """Run the bridge while retaining process-level failures."""
        try:
            cli._serve_mcp("mcp_1234", input_stream, output_stream)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    input_stream.send(_initialize_frame("init"))
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        }
    )

    response = _wait_for_host_response(output_stream, "call")
    assert response == {
        "jsonrpc": "2.0",
        "id": "call",
        "error": {
            "code": -32000,
            "message": "the Recurse service returned an invalid MCP task response",
        },
    }
    assert thread.is_alive()
    input_stream.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
    assert _wait_for_host_response(output_stream, "list") is not None
    input_stream.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []


def test_mcp_bridge_hides_an_unexpected_worker_failure(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected worker exception becomes a safe request-level error."""

    def fail_call(*args: object) -> tuple[dict[str, Any], str]:
        """Raise an implementation error that must not cross the bridge."""
        del args
        raise RuntimeError("private detail")

    monkeypatch.setattr(cli, "_call_mcp_tool", fail_call)
    service.mcp_responses = [_remote_discovery_reply(1)]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32603, "message": "internal error"},
    }


def test_mcp_bridge_contains_a_bad_resource_read_while_a_call_is_active(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid resource parameters do not terminate or abandon another call."""
    started = threading.Event()
    stopped = threading.Event()

    def blocked_call(
        deployment_id: str,
        message: dict[str, Any],
        access_token: str,
        cancelled: threading.Event,
    ) -> tuple[dict[str, Any] | None, str]:
        """Hold a call until the host cancels it."""
        del deployment_id, message
        started.set()
        assert cancelled.wait(1)
        stopped.set()
        return None, access_token

    monkeypatch.setattr(cli, "_call_mcp_tool", blocked_call)
    service.mcp_responses = [_remote_discovery_reply("init")]
    input_stream = _StreamingInput()
    output_stream = io.BytesIO()
    errors: list[BaseException] = []

    def serve() -> None:
        """Run the bridge while retaining process-level failures."""
        try:
            cli._serve_mcp("mcp_1234", input_stream, output_stream)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    input_stream.send(_initialize_frame("init"))
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        }
    )
    assert started.wait(1)
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "id": "resource",
            "method": "resources/read",
            "params": {
                "uri": (
                    "recurse://artifact/77777777-7777-4777-8777-777777777777/"
                    "99999999-9999-4999-8999-999999999999/"
                )
            },
        }
    )

    response = _wait_for_host_response(output_stream, "resource")
    assert response == {
        "jsonrpc": "2.0",
        "id": "resource",
        "error": {"code": -32602, "message": "invalid artifact resource URI"},
    }
    assert thread.is_alive()
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "call"},
        }
    )
    assert stopped.wait(1)
    input_stream.close()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert errors == []


def test_mcp_bridge_cancels_active_calls_before_a_fatal_dispatch_exit(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fatal main-loop failure cancels and joins every active remote call."""
    task_id = "task_" + "8" * 32
    service.mcp_live_task_id = task_id
    service.mcp_responses = [
        _remote_discovery_reply("init"),
        _remote_reply(
            "call",
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 10,
            },
        ),
    ]
    monkeypatch.setattr(cli, "_MCP_TASK_TIMEOUT_SECONDS", 0.2)

    def fail_list(*args: object) -> tuple[dict[str, Any], str]:
        """Simulate a malformed remote response on the dispatch thread."""
        del args
        raise cli.ServiceError("invalid list response")

    monkeypatch.setattr(cli, "_list_mcp_tools", fail_list)
    input_stream = _StreamingInput()
    output_stream = io.BytesIO()
    errors: list[BaseException] = []

    def serve() -> None:
        """Run the bridge while retaining its fatal dispatch error."""
        try:
            cli._serve_mcp("mcp_1234", input_stream, output_stream)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    input_stream.send(_initialize_frame("init"))
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "id": "call",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        }
    )
    assert _wait_for_mcp_method(service, "tools/call")
    input_stream.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], cli.ServiceError)
    assert str(errors[0]) == "invalid list response"
    assert _wait_for_mcp_method(service, "tasks/cancel", timeout=0.1)
    assert _wait_for_host_response(output_stream, "call", timeout=0.05) is None


def test_mcp_bridge_keeps_dispatching_while_an_artifact_download_blocks(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow artifact download does not block unrelated host requests."""
    started = threading.Event()
    release = threading.Event()

    def blocked_read(message: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Hold one resource read until another request is served."""
        started.set()
        assert release.wait(1)
        return {"jsonrpc": "2.0", "id": message["id"], "result": {}}, "access-2"

    monkeypatch.setattr(cli, "_read_mcp_resource", blocked_read)
    service.mcp_responses = [_remote_discovery_reply("init")]
    input_stream = _StreamingInput()
    output_stream = io.BytesIO()
    thread = threading.Thread(
        target=cli._serve_mcp,
        args=("mcp_1234", input_stream, output_stream),
    )
    thread.start()
    input_stream.send(_initialize_frame("init"))
    input_stream.send(
        {
            "jsonrpc": "2.0",
            "id": "resource",
            "method": "resources/read",
            "params": {
                "uri": (
                    "recurse://artifact/77777777-7777-4777-8777-777777777777/"
                    "99999999-9999-4999-8999-999999999999"
                )
            },
        }
    )
    assert started.wait(1)
    input_stream.send({"jsonrpc": "2.0", "id": "list", "method": "resources/list", "params": {}})

    assert _wait_for_host_response(output_stream, "list") is not None
    release.set()
    input_stream.close()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_mcp_bridge_ignores_unknown_and_malformed_cancellation_notifications(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Cancellation notifications without an active request remain fire-and-forget."""
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": []},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "already-finished"},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert [json.loads(line)["id"] for line in output_stream.getvalue().splitlines()] == [1]
    assert all(headers["Mcp-Method"] != "tasks/cancel" for headers in service.mcp_headers)


def test_mcp_bridge_rejects_a_duplicate_in_flight_request_id(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One host request id cannot identify two simultaneously active calls."""
    started = threading.Event()
    stopped = threading.Event()

    def blocked_call(
        deployment_id: str,
        message: dict[str, Any],
        access_token: str,
        cancelled: threading.Event,
    ) -> tuple[dict[str, Any] | None, str]:
        """Hold the first call active while the duplicate frame arrives."""
        del deployment_id, message
        started.set()
        assert cancelled.wait(1)
        stopped.set()
        return None, access_token

    monkeypatch.setattr(cli, "_call_mcp_tool", blocked_call)
    input_stream = _StreamingInput()
    output_stream = io.BytesIO()
    errors: list[BaseException] = []

    def serve() -> None:
        """Run the bridge and retain the duplicate-id failure."""
        try:
            cli._serve_mcp("mcp_1234", input_stream, output_stream)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    input_stream.send(_initialize_frame("init"))
    call = {
        "jsonrpc": "2.0",
        "id": "same-id",
        "method": "tools/call",
        "params": {"name": "tune", "arguments": {}},
    }
    input_stream.send(call)
    assert started.wait(1)
    input_stream.send(call)
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert stopped.is_set()
    assert len(errors) == 1
    assert isinstance(errors[0], cli._CliError)
    assert str(errors[0]) == "duplicate in-flight JSON-RPC request id"


def test_mcp_tool_call_requires_a_host_request_id() -> None:
    """A direct bridge call without an id fails before network traffic."""
    with pytest.raises(cli._CliError, match="malformed JSON-RPC frame"):
        cli._call_mcp_tool("mcp_1234", {}, "access-token", threading.Event())


def test_mcp_bridge_tolerates_transient_unavailability_only_while_polling(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Transient polling failures neither reauthenticate nor resubmit the tool call."""
    task_id = "task_" + "e" * 32
    call_result: dict[str, Any] = {
        "content": [{"type": "text", "text": "done after rollout"}],
        "isError": False,
    }
    service.mcp_responses = [
        _remote_discovery_reply(1),
        _remote_reply(
            2,
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 5,
            },
        ),
        _remote_reply(
            2,
            {
                "resultType": "complete",
                "taskId": task_id,
                "status": "completed",
                "result": call_result,
            },
        ),
    ]
    service.mcp_poll_failures = [
        (502, {"detail": "temporary gateway failure"}),
        (503, {"detail": "temporarily unavailable"}),
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
    )

    started = time.monotonic()
    cli._serve_mcp("mcp_1234", input_stream, output_stream)
    elapsed = time.monotonic() - started

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            **call_result,
            "content": [*call_result["content"], {"type": "text", "text": f"Task: {task_id}"}],
        },
    }
    assert 0.015 <= elapsed < 1
    assert [headers["Mcp-Method"] for headers in service.mcp_headers] == [
        "server/discover",
        "tools/call",
        "tasks/get",
        "tasks/get",
        "tasks/get",
    ]
    assert sum(headers["Mcp-Method"] == "tools/call" for headers in service.mcp_headers) == 1
    assert service.device_grants == ["device-1"]


@pytest.mark.parametrize("is_error", [False, True])
def test_mcp_bridge_retains_task_identity_without_changing_declared_output(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    is_error: bool,
) -> None:
    """Terminal results retain public identity but not private task-envelope fields."""
    task_id = "task_" + "b" * 32
    failure: dict[str, Any] = {
        "content": [
            {"type": "text", "text": "Run: 11111111-1111-4111-8111-111111111111."},
            {"type": "resource_link", "name": "receipt.json", "uri": "recurse://fixture"},
        ],
        "structuredContent": {"score": 0.75},
        "isError": is_error,
        "_meta": {"recurse/model": "public-model"},
    }
    service.mcp_responses = [
        _remote_discovery_reply(1),
        _remote_reply(
            2,
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 0,
            },
        ),
        _remote_reply(
            2,
            {
                "resultType": "complete",
                "taskId": task_id,
                "status": "completed",
                "result": failure,
                "private_trace": "must-not-be-forwarded",
            },
        ),
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {
            **failure,
            "content": [*failure["content"], {"type": "text", "text": f"Task: {task_id}"}],
        },
    }
    assert [headers["Mcp-Method"] for headers in service.mcp_headers] == [
        "server/discover",
        "tools/call",
        "tasks/get",
    ]
    assert json.loads(service.mcp_bodies[-1])["params"]["taskId"] == task_id


@pytest.mark.parametrize(
    ("task_result", "expected_error"),
    [
        (
            {"status": "cancelled"},
            {"code": -32800, "message": "tool call was cancelled"},
        ),
        (
            {
                "status": "failed",
                "statusMessage": "execution infrastructure failed",
                "error": {"code": -32000, "message": "execution infrastructure failed"},
            },
            {"code": -32000, "message": "execution infrastructure failed"},
        ),
    ],
)
def test_mcp_bridge_returns_terminal_task_errors_with_the_host_id(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    task_result: dict[str, Any],
    expected_error: dict[str, Any],
) -> None:
    """Cancellation and infrastructure failure become clear standard JSON-RPC errors."""
    task_id = "task_" + "c" * 32
    service.mcp_responses = [
        _remote_discovery_reply("init-id"),
        _remote_reply(
            "call-id",
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 0,
            },
        ),
        _remote_reply(
            "call-id",
            {"resultType": "complete", "taskId": task_id} | task_result,
        ),
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame("init-id"),
        {
            "jsonrpc": "2.0",
            "id": "call-id",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": "call-id",
        "error": {**expected_error, "message": f"{expected_error['message']} Task: {task_id}"},
    }


def test_mcp_bridge_bounds_task_polling(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task cannot keep a local host call waiting beyond the public timeout."""
    task_id = "task_" + "d" * 32
    service.mcp_responses = [
        _remote_discovery_reply(1),
        _remote_reply(
            2,
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 1000,
            },
        ),
    ]
    monkeypatch.setattr(cli, "_MCP_TASK_TIMEOUT_SECONDS", 0, raising=False)
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32000, "message": f"tool call timed out Task: {task_id}"},
    }
    assert len(service.mcp_bodies) == 2


def test_mcp_bridge_waits_through_the_platform_run_deadline(
    service: FakeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge remains attached near the end of a fifteen-minute run."""
    task_id = "task_" + "d" * 32
    service.mcp_responses = [
        _remote_reply(
            "call-id",
            {
                "resultType": "task",
                "taskId": task_id,
                "status": "working",
                "pollIntervalMs": 0,
            },
        ),
        _remote_reply(
            "call-id",
            {
                "resultType": "complete",
                "taskId": task_id,
                "status": "completed",
                "result": {"content": []},
            },
        ),
    ]

    moments = iter([0.0, 899.0])
    monkeypatch.setattr("_recurse_cli.time.monotonic", lambda: next(moments))

    response, token = cli._call_mcp_tool(
        "mcp_1234",
        {
            "jsonrpc": "2.0",
            "id": "call-id",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
        "access-1",
        threading.Event(),
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": "call-id",
        "result": {"content": [{"type": "text", "text": f"Task: {task_id}"}]},
    }
    assert token == "access-1"  # noqa: S105 - inert authentication fixture
    assert [headers["Mcp-Method"] for headers in service.mcp_headers] == [
        "tools/call",
        "tasks/get",
    ]


@pytest.mark.parametrize(
    "operation",
    [
        (
            "tools/list",
            {},
            [
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32602, "message": "list failed"},
                }
            ],
            "list failed",
        ),
        (
            "tools/call",
            {"name": "fail-now"},
            [
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32602, "message": "call failed"},
                }
            ],
            "call failed",
        ),
        (
            "tools/call",
            {"name": "fail-later"},
            [
                _remote_reply(
                    2,
                    {
                        "resultType": "task",
                        "taskId": "task_" + "f" * 32,
                        "status": "working",
                        "pollIntervalMs": 0,
                    },
                ),
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32000, "message": "poll failed"},
                },
            ],
            "poll failed Task: task_" + "f" * 32,
        ),
    ],
)
def test_mcp_bridge_translates_remote_errors_for_each_forwarded_operation(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    operation: tuple[str, dict[str, Any], list[dict[str, Any]], str],
) -> None:
    """Remote JSON-RPC errors retain each host id without becoming process errors."""
    method, params, remote_responses, message = operation
    service.mcp_responses = [_remote_discovery_reply(1), *remote_responses]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    response = json.loads(output_stream.getvalue().splitlines()[-1])
    assert response == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": remote_responses[-1]["error"]["code"],
            "message": message,
        },
    }


def test_mcp_bridge_retains_the_admitted_task_reference_on_poll_errors(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A rejected poll preserves correlation and public error data without resubmission."""
    task_id = "task_" + "a" * 32
    public_error = {
        "code": -32004,
        "message": "Task status is unavailable.",
        "data": {"retryable": False},
    }
    service.mcp_responses = [
        _remote_discovery_reply("host-init"),
        _remote_reply(
            "host-call",
            {"taskId": task_id, "status": "working", "pollIntervalMs": 0},
        ),
        {
            "jsonrpc": "2.0",
            "id": "remote-poll",
            "error": public_error,
            "private_trace": "must-not-be-forwarded",
        },
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame("host-init"),
        {
            "jsonrpc": "2.0",
            "id": "host-call",
            "method": "tools/call",
            "params": {"name": "tune", "arguments": {}},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": "host-call",
        "error": {
            "code": -32004,
            "message": f"Task status is unavailable. Task: {task_id}",
            "data": {"retryable": False},
        },
    }
    assert [headers["Mcp-Method"] for headers in service.mcp_headers] == [
        "server/discover",
        "tools/call",
        "tasks/get",
    ]
    assert json.loads(service.mcp_bodies[-1])["params"]["taskId"] == task_id


@pytest.mark.parametrize("message", [None, {"private": "must-not-be-stringified"}])
def test_mcp_bridge_rejects_malformed_poll_error_messages(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    message: object,
) -> None:
    """Appending correlation must not turn a non-text error into public diagnostics."""
    service.mcp_responses = [
        _remote_discovery_reply(1),
        _remote_reply(
            2,
            {"taskId": "task_" + "a" * 32, "status": "working", "pollIntervalMs": 0},
        ),
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": message}},
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "tune"}},
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32000,
            "message": "the Recurse service returned an invalid MCP task response",
        },
    }


def test_mcp_bridge_translates_a_discovery_error_returned_with_http_400(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A JSON-RPC discovery error remains a host response even with an HTTP error status."""
    service.mcp_failures = [
        (400, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad init"}})
    ]
    input_stream, output_stream = _bridge_frames(_initialize_frame(1))

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue()) == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "bad init"},
    }


@pytest.mark.parametrize(
    "result",
    [
        {},
        {
            "capabilities": {"tools": []},
            "_meta": {"io.modelcontextprotocol/serverInfo": {}},
        },
        {"capabilities": {"tools": {}}, "_meta": []},
        {
            "capabilities": {"tools": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": []},
        },
    ],
)
def test_mcp_bridge_rejects_incomplete_discovery(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    result: dict[str, Any],
) -> None:
    """Initialization requires public tool capabilities and server identity."""
    service.mcp_responses = [_remote_reply(1, result)]
    input_stream, output_stream = _bridge_frames(_initialize_frame(1))

    with pytest.raises(cli.ServiceError, match="invalid MCP discovery response"):
        cli._serve_mcp("mcp_1234", input_stream, output_stream)


@pytest.mark.parametrize("interval", [True, "soon", -1])
def test_mcp_bridge_rejects_invalid_task_poll_intervals(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    interval: object,
) -> None:
    """Only non-negative integer task polling intervals are accepted."""
    service.mcp_responses = [
        _remote_discovery_reply(1),
        _remote_reply(
            2,
            {
                "taskId": "task_" + "1" * 32,
                "status": "working",
                "pollIntervalMs": interval,
            },
        ),
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tune"},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32000,
            "message": "the Recurse service returned an invalid MCP task response",
        },
    }


@pytest.mark.parametrize(
    "task_result",
    [
        {"taskId": "task_" + "2" * 32, "status": "completed"},
        {"taskId": "task_" + "2" * 32, "status": "completed", "result": {}},
        {"taskId": "task_" + "2" * 32, "status": "completed", "result": {"content": None}},
        {"taskId": "task_" + "3" * 32, "status": "failed"},
        {"taskId": "task_" + "3" * 32, "status": "failed", "error": {}},
        {"taskId": "task_" + "3" * 32, "status": "failed", "error": {"message": None}},
        {"taskId": "task_" + "4" * 32, "status": "unknown"},
        {"status": "working", "pollIntervalMs": 0},
    ],
)
def test_mcp_bridge_rejects_invalid_task_results(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    task_result: dict[str, Any],
) -> None:
    """Incomplete and unknown task results stop with a clear service error."""
    service.mcp_responses = [
        _remote_discovery_reply(1),
        _remote_reply(2, task_result),
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "tune"},
        },
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32000,
            "message": "the Recurse service returned an invalid MCP task response",
        },
    }


@pytest.mark.parametrize(
    "frame",
    [
        b"not-json\n",
        b"[]\n",
        b"{}\n",
        b'{"jsonrpc":"2.0"}\n',
        b'{"jsonrpc":"2.0","method":"initialize","params":{}}\n',
        b'{"jsonrpc":"2.0","method":"tools/list","params":{}}\n',
    ],
)
def test_mcp_bridge_rejects_malformed_frames(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    frame: bytes,
) -> None:
    """Malformed stdio frames stop locally without reaching the MCP service."""
    expected = "first MCP frame" if b"tools/list" in frame else "malformed JSON-RPC frame"
    with pytest.raises(cli._CliError, match=expected):
        cli._serve_mcp("mcp_1234", io.BytesIO(frame), io.BytesIO())

    assert service.mcp_bodies == []


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": []},
        {"jsonrpc": "2.0", "method": "resources/list", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list", "params": []},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/templates/list",
            "params": [],
        },
        {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "tune"}},
    ],
)
def test_mcp_bridge_rejects_malformed_requests_after_initialization(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    message: dict[str, Any],
) -> None:
    """Standard tool and resource requests require an id and object parameters."""
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        message,
    )

    with pytest.raises(cli._CliError, match="malformed JSON-RPC frame"):
        cli._serve_mcp("mcp_1234", input_stream, output_stream)


@pytest.mark.parametrize("params", [None, {"name": ""}, {"name": "bad\r\n"}, {"name": "tune-🧪"}])
def test_mcp_bridge_answers_malformed_tool_calls(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    params: dict[str, Any] | None,
) -> None:
    """A malformed call receives invalid-params without terminating the bridge."""
    service.mcp_responses = [_remote_discovery_reply(1)]
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": 2, "method": "tools/call"}
    if params is not None:
        message["params"] = params
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        message,
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert json.loads(output_stream.getvalue().splitlines()[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32602, "message": "malformed JSON-RPC frame on standard input"},
    }


def test_mcp_bridge_consumes_unknown_notifications_and_rejects_unknown_requests(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Unknown notifications stay local while unknown requests receive method-not-found."""
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1),
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
        {"jsonrpc": "2.0", "id": "unknown", "method": "prompts/list", "params": {}},
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    responses = [json.loads(line) for line in output_stream.getvalue().splitlines()]
    assert responses[-1] == {
        "jsonrpc": "2.0",
        "id": "unknown",
        "error": {"code": -32601, "message": "method not found"},
    }


def test_mcp_bridge_surfaces_service_errors(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A non-authentication HTTP failure becomes a concise public service error."""
    service.mcp_failures = [(503, {"detail": "deployment is unavailable"})]
    input_stream, output_stream = _bridge_frames(_initialize_frame(1, "2025-06-18"))

    with pytest.raises(cli.ServiceError, match="deployment is unavailable"):
        cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert output_stream.getvalue() == b""


def test_mcp_bridge_reauthenticates_once_after_401(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A 401 obtains one new short-lived token and retries the exact frame once."""
    service.token_responses = ["access-old", "access-new"]
    service.mcp_failures = [(401, {"detail": "access token expired"})]
    input_stream, output_stream = _bridge_frames(_initialize_frame(1, "2025-06-18"))

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert service.mcp_bodies[0] == service.mcp_bodies[1]
    assert [headers["Authorization"] for headers in service.mcp_headers] == [
        "Bearer access-old",
        "Bearer access-new",
    ]
    assert service.device_grants == ["device-1", "device-1"]
    assert json.loads(output_stream.getvalue())["id"] == 1


def test_mcp_bridge_reports_a_second_401_without_retrying_again(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Persistent authentication failure stops after the single permitted retry."""
    service.token_responses = ["access-old", "access-new"]
    service.mcp_failures = [
        (401, {"detail": "access token expired"}),
        (401, {"detail": "credential was revoked"}),
    ]
    input_stream, output_stream = _bridge_frames(_initialize_frame(1, "2025-06-18"))

    with pytest.raises(cli.ServiceError, match="credential was revoked"):
        cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert len(service.mcp_bodies) == 2
    assert service.device_grants == ["device-1", "device-1"]


@pytest.mark.parametrize(
    "raw_response",
    [b"", b'{"jsonrpc":"2.0","id":1}'],
)
def test_mcp_bridge_rejects_an_incomplete_success_response(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    raw_response: bytes,
) -> None:
    """An incomplete successful response cannot satisfy the adapter contract."""
    service.mcp_raw_responses = [raw_response]
    input_stream, output_stream = _bridge_frames(_initialize_frame(1, "2025-06-18"))

    with pytest.raises(cli.ServiceError, match="invalid MCP response"):
        cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert output_stream.getvalue() == b""


def test_mcp_bridge_accepts_newline_terminated_remote_responses(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Remote response delimiters do not create blank host frames."""
    service.mcp_raw_responses = [
        json.dumps(_remote_discovery_reply(1)).encode() + b"\n",
        json.dumps(_remote_reply(2, {"tools": []})).encode() + b"\n",
    ]
    input_stream, output_stream = _bridge_frames(
        _initialize_frame(1, "2025-06-18"),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    responses = output_stream.getvalue().splitlines()
    assert len(responses) == 2
    assert json.loads(responses[-1]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {"tools": []},
    }


def test_mcp_bridge_reports_an_unreachable_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bridge connection failure is reported as a public service error."""
    monkeypatch.setenv("RECURSE_API_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(cli, "_access_token", lambda: "short-lived-token")
    input_stream, output_stream = _bridge_frames(_initialize_frame(1, "2025-06-18"))

    with pytest.raises(cli.ServiceError, match="could not be reached"):
        cli._serve_mcp("mcp_1234", input_stream, output_stream)


def test_mcp_cli_dispatches_standard_input_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public mcp serve command connects the process binary streams to the bridge."""
    standard_input = io.TextIOWrapper(io.BytesIO())
    standard_output = io.TextIOWrapper(io.BytesIO())
    received: list[tuple[str, Any, Any]] = []

    def serve(deployment_id: str, input_stream: Any, output_stream: Any) -> None:
        """Record the parser dispatch without performing network traffic."""
        received.append((deployment_id, input_stream, output_stream))

    monkeypatch.setattr(sys, "stdin", standard_input)
    monkeypatch.setattr(sys, "stdout", standard_output)
    monkeypatch.setattr(cli, "_serve_mcp", serve)

    assert main(["mcp", "serve", "mcp_1234"]) == 0
    assert received == [("mcp_1234", standard_input.buffer, standard_output.buffer)]


def test_twelve_bridge_processes_share_one_access_token(
    service: FakeService,
    tmp_path: Path,
) -> None:
    """Independent bridge startups share one token without twelve provider exchanges."""
    env = process_environment(tmp_path, service.url)
    frame = json.dumps(_initialize_frame(1, "2025-06-18")).encode() + b"\n"
    command = [
        sys.executable,
        "-c",
        "from _recurse_cli import main; raise SystemExit(main(['mcp', 'serve', 'mcp_1234']))",
    ]
    processes = [
        subprocess.Popen(  # noqa: S603 - fixed current-interpreter test command
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
        )
        for _ in range(12)
    ]

    results = [process.communicate(frame, timeout=10) for process in processes]

    assert [process.returncode for process in processes] == [0] * 12, results
    assert [json.loads(stdout)["id"] for stdout, _ in results] == [1] * 12
    assert [stderr for _, stderr in results] == [b""] * 12
    assert service.device_grants == ["device-1"]


def test_mcp_bridge_never_outputs_or_forwards_the_device_credential(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """The durable credential stays between the keychain and token/logout routes."""
    input_stream, output_stream = _bridge_frames(_initialize_frame(1, "2025-06-18"))

    cli._serve_mcp("mcp_1234", input_stream, output_stream)

    assert b"device-1" not in output_stream.getvalue()
    assert all(b"device-1" not in body for body in service.mcp_bodies)
    assert all("device-1" not in json.dumps(headers) for headers in service.mcp_headers)
