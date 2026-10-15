"""Tools commands and stdio exercise the separate service over real HTTP."""

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import _recurse_cli as cli
from tests import test_cli
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService
from tests.test_tools_bundle import write_tools_service

_ephemeral_callback_port = test_cli._ephemeral_callback_port
keychain = test_cli.keychain
logged_in = test_cli.logged_in
service = test_cli.service


@pytest.fixture
def tools_service(service: FakeService, monkeypatch: pytest.MonkeyPatch) -> FakeService:
    """Route Tools HTTP/MCP separately while keeping the actual issuer exchange."""
    monkeypatch.setenv("RECURSE_TOOLS_URL", service.url)
    original = service.route

    def route(method: str, path: str, body: Any, token: str | None) -> tuple[int, dict[str, Any]]:
        """Return raw call receipts or ordinary MCP responses, never Tasks."""
        if path.startswith("/v1/oauth/"):
            return original(method, path, body, token)
        assert token == "Bearer access-1"  # noqa: S105 - synthetic bearer
        if path == "/mcp":
            assert isinstance(body, dict)
            result: dict[str, Any]
            assert body["params"]["_meta"] == {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "recurse-sdk", "version": "0.2.7"},
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            if body["method"] == "server/discover":
                result = {
                    "capabilities": {"tools": {}},
                    "_meta": {
                        "io.modelcontextprotocol/serverInfo": {
                            "name": "recurse-tools",
                            "version": "1",
                        }
                    },
                }
            elif body["method"] == "tools/list":
                result = {
                    "tools": [
                        {"name": name}
                        for name in (
                            "catalog_search",
                            "catalog_get",
                            "call",
                            "get_call",
                            "cancel_call",
                        )
                    ]
                }
            else:
                result = {
                    "content": [{"type": "text", "text": "accepted"}],
                    "structuredContent": {"call_id": "call-1", "status": "pending"},
                }
            return 200, {"jsonrpc": "2.0", "id": body["id"], "result": result}
        return 200, {"call_id": "call-1", "status": "failed", "error": {"code": "tool_error"}}

    monkeypatch.setattr(service, "route", route)
    return service


@pytest.mark.parametrize(
    "command,path,body",
    [
        (
            ["search", "two words", "--limit", "7", "--cursor", "opaque/+"],
            "/v1/catalog?search=two+words&limit=7&cursor=opaque%2F%2B",
            None,
        ),
        (["inspect", "fixture.echo"], "/v1/catalog/fixture.echo", None),
        (
            ["apis", "search", "search & extract", "--limit", "2"],
            "/v1/apis?query=search+%26+extract&limit=2",
            None,
        ),
        (["apis", "search"], "/v1/apis?query=&limit=20", None),
        (
            ["apis", "search", "--cursor", "opaque/+"],
            "/v1/apis?query=&limit=20&cursor=opaque%2F%2B",
            None,
        ),
        (["apis", "inspect", "exa.web.search"], "/v1/apis/exa.web.search", None),
        (["get", "call-1"], "/v1/calls/call-1", None),
        (["cancel", "call-1"], "/v1/calls/call-1/cancel", {}),
        (["version", "version-1"], "/v1/tool-versions/version-1", None),
    ],
)
def test_tools_commands_return_raw_json_receipts(  # noqa: PLR0913,PLR0917 - HTTP fixtures and wire parameters
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
    command: list[str],
    path: str,
    body: object,
) -> None:
    """Successful receipt reads remain command success even when execution failed."""
    del logged_in
    assert cli.main(["tools", *command]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "call_id": "call-1",
        "status": "failed",
        "error": {"code": "tool_error"},
    }
    assert output.err == ""
    assert tools_service.requests[-1][1:3] == (path, body)
    assert tools_service.requests[0][2] == {
        "grant_type": "refresh_token",
        "client_id": "recurse-cli",
        "refresh_token": "device-1",
        "resource": tools_service.url + "/mcp",
    }


def test_tools_call_requires_explicit_versions_cap_and_idempotency(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI admission sends the owner's exact approval and JSON input unchanged."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"message":"hello"}'))
    assert (
        cli.main(
            [
                "tools",
                "call",
                "fixture.echo",
                "--tool-version",
                "version-1",
                "--price-version",
                "price-1",
                "--inputs",
                "-",
                "--idempotency-key",
                "request-1",
                "--max-cost-micro-usd",
                "100000",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["call_id"] == "call-1"
    assert tools_service.requests[-1][1:3] == (
        "/v1/calls",
        {
            "tool_id": "fixture.echo",
            "tool_version": "version-1",
            "price_version": "price-1",
            "inputs": {"message": "hello"},
            "idempotency_key": "request-1",
            "max_cost_micro_usd": 100000,
        },
    )


def test_tools_publish_version_reads_config_file(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resumable publication receives configuration through a file, never secret flags."""
    del logged_in
    config = tmp_path / "config.json"
    config.write_text('{"tools":{}}')
    assert cli.main(["tools", "publish-version", "version-1", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["call_id"] == "call-1"
    assert tools_service.requests[-1][1:3] == (
        "/v1/tool-versions/version-1/publication",
        {"tools": {}},
    )


@pytest.mark.parametrize("text", ["[]", "{", '{"value":NaN}', '{"value":Infinity}'])
def test_tools_invalid_json_does_not_authenticate_or_admit(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    text: str,
) -> None:
    """Invalid local JSON produces a bounded CLI error before any service request."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    assert cli.main(["tools", "publish-version", "version-1", "--config", "-"]) == 2
    assert tools_service.requests == []
    assert "readable JSON object" in capsys.readouterr().err


def _frame(method: str, request_id: object = 1, **params: Any) -> bytes:
    """Encode one standard host request."""
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
    ).encode()


def _initialize() -> bytes:
    """Encode the standard initialization expected by the separate service."""
    return _frame(
        "initialize",
        protocolVersion="2025-11-25",
        capabilities={},
        clientInfo={"name": "test-host", "version": "1"},
    )


def test_tools_stdio_is_ordinary_mcp_and_preserves_request_ids(
    tools_service: FakeService, logged_in: dict[tuple[str, str], str]
) -> None:
    """An accepted call returns immediately; subsequent get is a separate tool request."""
    del logged_in
    output = io.BytesIO()
    cli._serve_tools_mcp(
        [
            _initialize(),
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            _frame("tools/list", "list-id"),
            _frame("tools/call", "call-id", name="call", arguments={"idempotency_key": "same"}),
            _frame("tools/call", "get-id", name="get_call", arguments={"call_id": "call-1"}),
            _frame("tasks/get", "task-id", taskId="call-1"),
        ],
        output,
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [
        1,
        "list-id",
        "call-id",
        "get-id",
        "task-id",
    ]
    assert responses[0]["result"]["capabilities"] == {"tools": {}}
    assert responses[2]["result"]["structuredContent"]["status"] == "pending"
    assert responses[4]["error"]["code"] == -32601
    remote = [body for _, path, body, _ in tools_service.requests if path == "/mcp"]
    assert [body["method"] for body in remote if isinstance(body, dict)] == [
        "server/discover",
        "tools/list",
        "tools/call",
        "tools/call",
    ]
    assert b"device-1" not in output.getvalue()
    assert b"access-1" not in output.getvalue()


def test_tools_stdio_subprocess_keeps_stdout_protocol_only(
    tools_service: FakeService,
    tmp_path: Path,
) -> None:
    """The actual console entry point uses isolated keyring storage and Tools audience."""
    env = process_environment(tmp_path, tools_service.url)
    env["RECURSE_TOOLS_URL"] = tools_service.url
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from _recurse_cli import main; raise SystemExit(main())",
            "tools",
            "mcp",
            "serve",
        ],
        input=_initialize() + b"\n" + _frame("tools/list", "list-id") + b"\n",
        env=env,
        capture_output=True,
        check=True,
    )
    assert result.stderr == b""
    assert [json.loads(line)["id"] for line in result.stdout.splitlines()] == [1, "list-id"]
    assert tools_service.requests[0][2] == {
        "grant_type": "refresh_token",
        "client_id": "recurse-cli",
        "refresh_token": "device-1",
        "resource": tools_service.url + "/mcp",
    }


@pytest.mark.parametrize(
    "frame",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"jsonrpc":"2.0","method":"tools/list","id":1}',
        b'{"jsonrpc":"2.0","method":"initialize","id":null}',
        b'{"jsonrpc":"2.0","method":"initialize","id":true}',
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
    ],
)
def test_tools_stdio_rejects_malformed_host_frames(frame: bytes) -> None:
    """No malformed host frame is turned into an authenticated remote operation."""
    with pytest.raises(cli._CliError, match=r"frame|first|request id"):
        cli._serve_tools_mcp([frame], io.BytesIO())


def test_tools_stdio_reports_missing_login_inside_initialize(
    service: FakeService,
    keychain: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host receives one safe protocol error without browser login or a leaked token."""
    del keychain
    monkeypatch.setenv("RECURSE_TOOLS_URL", service.url)
    output = io.BytesIO()
    cli._serve_tools_mcp([_initialize(), _frame("tools/list", 2)], output)
    response = json.loads(output.getvalue())
    assert response["id"] == 1
    assert response["error"]["code"] == -32000
    assert "recurse login" in response["error"]["message"]
    assert service.requests == []


def test_tools_stdio_validates_params_and_ignores_notifications(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Invalid params yield one host-addressed error while unknown notifications are ignored."""
    del logged_in
    output = io.BytesIO()
    cli._serve_tools_mcp(
        [
            _initialize(),
            b'{"jsonrpc":"2.0","method":"tools/list","id":"bad","params":[]}',
            b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":1}}',
            _frame("tools/list", "good"),
        ],
        output,
    )
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, "bad", "good"]
    assert responses[1]["error"]["code"] == -32602
    assert len([item for item in tools_service.requests if item[1] == "/mcp"]) == 2


@pytest.mark.parametrize(
    "response",
    [
        {"jsonrpc": "2.0", "id": "wrong", "result": {}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1},
        {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": "wrong", "message": "failure"}},
    ],
)
def test_tools_stdio_contains_malformed_remote_responses(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    response: dict[str, Any],
) -> None:
    """Malformed service replies become safe host-addressed protocol errors."""
    del logged_in
    tools_service.malformed["/mcp"] = response
    output = io.BytesIO()
    cli._serve_tools_mcp([_initialize()], output)
    parsed = json.loads(output.getvalue())
    assert parsed["id"] == 1
    assert parsed["error"]["code"] == -32000
    assert "invalid MCP response" in parsed["error"]["message"]


def test_tools_stdio_preserves_remote_tool_errors(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """The remote safe JSON-RPC error retains its host request identity."""
    del logged_in
    tools_service.malformed["/mcp"] = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "invalid request"},
    }
    output = io.BytesIO()
    cli._serve_tools_mcp([_initialize()], output)
    assert json.loads(output.getvalue())["error"] == {"code": -32602, "message": "invalid request"}


def test_tools_json_file_errors_are_cli_errors(
    service: FakeService,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unreadable publication configuration stops before authentication."""
    assert (
        cli.main(["tools", "publish-version", "version-1", "--config", str(tmp_path / "missing")])
        == 2
    )
    assert service.requests == []
    assert "readable JSON object" in capsys.readouterr().err


def test_tools_help_requires_explicit_call_approval(capsys: pytest.CaptureFixture[str]) -> None:
    """Call command has no optional versions, cap, inputs, or generated request key."""
    with pytest.raises(SystemExit) as exit_status:
        cli.main(["tools", "call", "--help"])
    assert exit_status.value.code == 0
    help_text = capsys.readouterr().out
    for field in (
        "--tool-version",
        "--price-version",
        "--inputs",
        "--idempotency-key",
        "--max-cost-micro-usd",
    ):
        assert field in help_text


@pytest.mark.parametrize("configured", [False, True])
def test_tools_publish_builds_uploads_and_publishes_once(  # noqa: PLR0913,PLR0917 - HTTP fixtures plus configuration
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configured: bool,
) -> None:
    """The CLI bundles a real tools.yaml service and exposes only the publication receipt."""
    del logged_in
    directory = write_tools_service(tmp_path / "source")
    config = (
        {"tools": {"echo": {"billing": {"provider_rate_version": "provider-none-v1"}}}}
        if configured
        else {"tools": {}}
    )
    original = tools_service.route

    def route(method: str, path: str, body: Any, token: str | None) -> tuple[int, dict[str, Any]]:
        """Implement the immutable publisher flow and opaque source capability."""
        if path == "/v1/tool-versions":
            return 201, {
                "version_id": "version-1",
                "source_upload": tools_service.registration["source_upload"],
            }
        if path == "/direct-upload":
            return 201, {}
        if path == "/v1/tool-versions/version-1/complete":
            assert body == {"source_upload_id": "source-upload-9"}
            return 200, {"version_id": "version-1", "status": "preparing"}
        if path == "/v1/tool-versions/version-1":
            return 200, {"version_id": "version-1", "status": "prepared"}
        if path == "/v1/tool-versions/version-1/publication":
            assert body == config
            return 200, {"version_id": "version-1", "status": "published"}
        return original(method, path, body, token)

    monkeypatch.setattr(tools_service, "route", route)
    command = ["tools", "publish", str(directory), "--timeout-seconds", "60"]
    if configured:
        command += ["--config", "-"]
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(config)))
    assert cli.main(command) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"version_id": "version-1", "status": "published"}
    assert output.err == ""
    uploads = [item for item in tools_service.requests if item[1] == "/direct-upload"]
    assert len(uploads) == 1
    assert uploads[0][3] == "Bearer source-upload"
    assert tools_service.direct_upload_user_agent == "recurse-sdk"
    assert len([item for item in tools_service.requests if item[1] == "/v1/tool-versions"]) == 1


def test_tools_mcp_cli_dispatches_binary_streams(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Tools CLI selects its own ordinary bridge without the agent deployment bridge."""
    del logged_in
    input_stream = io.TextIOWrapper(io.BytesIO(_initialize() + b"\n"))
    output_stream = io.TextIOWrapper(io.BytesIO())
    monkeypatch.setattr(sys, "stdin", input_stream)
    monkeypatch.setattr(sys, "stdout", output_stream)
    assert cli.main(["tools", "mcp", "serve"]) == 0
    assert (
        json.loads(output_stream.buffer.getvalue())["result"]["serverInfo"]["name"]
        == "recurse-tools"
    )


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"capabilities": {}, "_meta": {}},
        {"capabilities": {"tools": []}, "_meta": {"io.modelcontextprotocol/serverInfo": {}}},
        {"capabilities": {"tools": {}}, "_meta": {"io.modelcontextprotocol/serverInfo": []}},
    ],
)
def test_tools_stdio_rejects_incomplete_discovery(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    result: dict[str, Any],
) -> None:
    """Host initialization cannot succeed without ordinary Tools discovery metadata."""
    del logged_in
    tools_service.malformed["/mcp"] = {"jsonrpc": "2.0", "id": 1, "result": result}
    output = io.BytesIO()
    cli._serve_tools_mcp([_initialize()], output)
    response = json.loads(output.getvalue())
    assert response["error"]["code"] == -32000
    assert "discovery" in response["error"]["message"]


@pytest.mark.parametrize("params", [{}, {"protocolVersion": 1}, {"protocolVersion": ""}])
def test_tools_stdio_rejects_invalid_initialization(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    params: dict[str, Any],
) -> None:
    """The ordinary handshake requires a host protocol version before remote discovery."""
    del logged_in
    output = io.BytesIO()
    cli._serve_tools_mcp([_frame("initialize", **params)], output)
    response = json.loads(output.getvalue())
    assert response["error"]["code"] == -32000
    assert "initialize frame" in response["error"]["message"]
    assert not any(path == "/mcp" for _, path, _, _ in tools_service.requests)


def test_tools_stdio_contains_unexpected_internal_errors(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected internal bug cannot expose private diagnostic values to a host."""
    del tools_service, logged_in

    def failure(*_args: object) -> dict[str, Any]:
        """Represent an unforeseen transport implementation failure."""
        raise RuntimeError("private token should never reach stdout")

    monkeypatch.setattr(cli, "_tools_mcp_response", failure)
    output = io.BytesIO()
    cli._serve_tools_mcp([_initialize()], output)
    assert json.loads(output.getvalue())["error"] == {"code": -32603, "message": "internal error"}


@pytest.mark.parametrize("operation", ["call", "publish-version"])
def test_deep_json_is_a_safe_cli_error_before_authentication(
    tools_service: FakeService,
    tmp_path: Path,
    operation: str,
) -> None:
    """Valid JSON exceeding Python nesting limits cannot leak an uncaught traceback."""
    env = process_environment(tmp_path, tools_service.url)
    env["RECURSE_TOOLS_URL"] = tools_service.url
    if operation == "call":
        arguments = [
            "tools",
            "call",
            "fixture.echo",
            "--tool-version",
            "version-1",
            "--price-version",
            "price-1",
            "--idempotency-key",
            "deep-json",
            "--max-cost-micro-usd",
            "100000",
            "--inputs",
            "-",
        ]
    else:
        arguments = ["tools", "publish-version", "version-1", "--config", "-"]
    nested = '{"value":' * 100000 + "{}" + "}" * 100000
    result = subprocess.run(  # noqa: S603 - fixed CLI regression commands
        [
            sys.executable,
            "-c",
            "from _recurse_cli import main; raise SystemExit(main())",
            *arguments,
        ],
        input=nested.encode(),
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"readable JSON object" in result.stderr
    assert b"Traceback" not in result.stderr
    assert tools_service.requests == []
