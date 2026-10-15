"""Native MCP preserves private project authority while source bytes use the scoped HTTP grant."""

import base64
import hashlib
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

import _recurse_cli as cli
import recurse
from tests.test_cli import FakeService
from tests.test_tools_bundle import write_tools_service
from tests.test_tools_cli import _frame, _initialize, keychain, logged_in, service, tools_service

__all__ = ["keychain", "logged_in", "service", "tools_service"]

_OPERATIONS = (
    "catalog_search",
    "catalog_get",
    "call",
    "get_call",
    "cancel_call",
    "apis_search",
    "apis_get",
    "private_stage",
    "private_complete",
    "private_get_version",
    "private_inspect",
    "private_replay",
    "private_call",
)


@pytest.mark.parametrize("login_kind", ["cli", "key"])
def test_native_mcp_transports_the_private_source_workflow_and_dynamic_api_catalog(  # noqa: PLR0915 - complete source HTTP transfer and thirteen MCP operations
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    login_kind: str,
) -> None:
    """One unchanged bridge discovers thirteen operations and uploads a larger bundle over HTTP."""
    if login_kind == "key":
        logged_in.clear()
        key = "rck_" + "a" * 43

        def oauth(path: str, body: dict[str, Any] | bytes | None) -> tuple[int, dict[str, Any]]:
            """Issue the customer key's Tools bearer through the actual HTTP exchange."""
            assert path == "/v1/oauth/token"
            assert body == {
                "grant_type": "refresh_token",
                "client_id": "recurse-tools-key",
                "refresh_token": key,
                "resource": tools_service.url + "/mcp",
            }
            return 200, {"access_token": "access-1", "token_type": "Bearer", "expires_in": 3600}

        monkeypatch.setattr(tools_service, "oauth", oauth)
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(key.encode())))
        assert cli.main(["tools", "login", "--key-stdin"]) == 0
    directory = write_tools_service(tmp_path / "project")
    metadata_path = directory / "pyproject.toml"
    metadata_path.write_text(
        metadata_path.read_text().replace("include = [", 'include = ["blob.bin", ')
    )
    (directory / "blob.bin").write_bytes(
        hashlib.shake_256(b"source-size-boundary").digest(70 * 1024)
    )
    artifacts, record = recurse.build_tools_bundle(directory)
    assert len(artifacts["source"]) > 64 * 1024
    descriptor = tools_service.registration["source_upload"]
    expected_registration = {
        "service_name": "fixture-tools",
        "application_record": record,
        "max_cost_micro_usd": 200_000,
    }
    identity = {
        "service_name": "fixture-tools",
        "source_sha256": record["source"]["sha256"],
        "function_name": "echo",
        "inputs": {"message": "hello"},
        "idempotency_key": "native-private-key",
        "max_cost_micro_usd": 200_000,
    }
    admission = {
        "version_id": "version-1",
        "function_name": "echo",
        "tool_version": "tool-v1",
        "price_version": "price-v1",
        "inputs": identity["inputs"],
        "idempotency_key": identity["idempotency_key"],
        "max_cost_micro_usd": 200_000,
    }
    api_items = [{"id": "exa.web.search"}, {"id": "firecrawl.web.scrape"}]
    stored_source: list[bytes] = []
    admitted: list[dict[str, Any]] = []
    receipt = {"call_id": "private-call-1", "status": "pending", "billing_status": "reserved"}
    original = tools_service.route

    def route(  # noqa: PLR0912 - explicit scoped HTTP upload and ordinary MCP protocol cases
        method: str, path: str, body: Any, bearer: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Emulate the server contract without making the native client interpret provider IDs."""
        if path == "/direct-upload":
            assert bearer == descriptor["upload_authorization"]
            assert isinstance(body, bytes)
            prefix = base64.b64decode(descriptor["upload_prefix"])
            suffix = base64.b64decode(descriptor["upload_suffix"])
            stored_source.append(body.removeprefix(prefix).removesuffix(suffix))
            return 201, {}
        if path != "/mcp" or body["method"] == "server/discover":
            return original(method, path, body, bearer)
        assert bearer == "Bearer access-1"
        assert body["params"]["_meta"]["io.modelcontextprotocol/clientInfo"] == {
            "name": "recurse-sdk",
            "version": "0.2.7",
        }
        if body["method"] == "tools/list":
            result: dict[str, Any] = {"tools": [{"name": name} for name in _OPERATIONS]}
        else:
            assert body["method"] == "tools/call"
            name = body["params"]["name"]
            arguments = body["params"]["arguments"]
            payload: dict[str, Any]
            if name == "apis_search":
                payload = {"items": list(api_items), "cursor": None}
            elif name == "apis_get":
                assert arguments["endpoint_id"] == api_items[-1]["id"]
                payload = {
                    "id": arguments["endpoint_id"],
                    "version": "server-version",
                    "max_cost_micro_usd": 5000,
                }
            elif name == "private_stage":
                assert arguments == expected_registration
                payload = {
                    "version_id": "version-1",
                    "status": "staged",
                    "source_upload": descriptor,
                }
            elif name == "private_complete":
                assert arguments == {
                    "version_id": "version-1",
                    "source_upload_id": descriptor["upload_id"],
                }
                assert stored_source == [artifacts["source"]]
                payload = {"version_id": "version-1", "status": "pending"}
            elif name == "private_get_version":
                assert arguments == {"version_id": "version-1"}
                payload = {"version_id": "version-1", "status": "prepared"}
            elif name == "private_inspect":
                assert arguments == {"version_id": "version-1", "function_name": "echo"}
                payload = {"version": "tool-v1", "price_version": "price-v1"}
            elif name == "private_replay":
                assert arguments == identity
                payload = {"receipt": receipt if admitted else None}
            elif name == "private_call":
                assert arguments == admission
                admitted.append(arguments)
                payload = receipt
            else:
                assert name in {"catalog_search", "catalog_get", "call", "get_call", "cancel_call"}
                payload = receipt
            result = {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload,
            }
        return 200, {"jsonrpc": "2.0", "id": body["id"], "result": result}

    monkeypatch.setattr(tools_service, "route", route)
    output = io.BytesIO()

    def latest_payload() -> dict[str, Any]:
        """Read the native host's last completed ordinary structured tool result."""
        return cast(
            "dict[str, Any]",
            json.loads(output.getvalue().splitlines()[-1])["result"]["structuredContent"],
        )

    def frames() -> Iterator[bytes]:
        """Advance private authoring using returned grants and metadata."""
        yield _initialize()
        yield _frame("tools/list", "operations-before")
        yield _frame(
            "tools/call", "apis-before", name="apis_search", arguments={"query": "", "limit": 20}
        )
        assert len(latest_payload()["items"]) == 2
        api_items.append({"id": "new-provider.read"})
        yield _frame(
            "tools/call",
            "apis-after",
            name="apis_search",
            arguments={"query": "", "limit": 20, "cursor": "opaque"},
        )
        assert len(latest_payload()["items"]) == 3
        discovered_id = latest_payload()["items"][-1]["id"]
        yield _frame(
            "tools/call",
            "api-definition",
            name="apis_get",
            arguments={"endpoint_id": discovered_id},
        )
        assert latest_payload()["id"] == discovered_id
        yield _frame("tools/call", "replay-before", name="private_replay", arguments=identity)
        assert latest_payload()["receipt"] is None
        yield _frame(
            "tools/call", "source-stage", name="private_stage", arguments=expected_registration
        )
        grant = latest_payload()["source_upload"]
        # The native client performs this HTTP transfer; it is never an MCP source frame.
        recurse.ToolsClient("unused-for-upload", base_url=tools_service.url)._upload_source(
            grant, artifacts["source"]
        )
        yield _frame(
            "tools/call",
            "source-complete",
            name="private_complete",
            arguments={"version_id": "version-1", "source_upload_id": grant["upload_id"]},
        )
        yield _frame(
            "tools/call",
            "preparation",
            name="private_get_version",
            arguments={"version_id": "version-1"},
        )
        yield _frame(
            "tools/call",
            "definition",
            name="private_inspect",
            arguments={"version_id": "version-1", "function_name": "echo"},
        )
        yield _frame("tools/call", "execution", name="private_call", arguments=admission)
        assert latest_payload() == receipt
        yield _frame("tools/call", "replay-after", name="private_replay", arguments=identity)
        assert latest_payload()["receipt"] == receipt
        for name in ("catalog_search", "catalog_get", "call", "get_call", "cancel_call"):
            yield _frame("tools/call", name, name=name, arguments={"owner_test": True})
        yield _frame("tools/list", "operations-after")

    cli._serve_tools_mcp(frames(), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[1]["result"] == responses[-1]["result"]
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == list(_OPERATIONS)
    assert len(admitted) == 1
    assert stored_source == [artifacts["source"]]
    remote = [body for _, path, body, _ in tools_service.requests if path == "/mcp"]
    assert all(
        isinstance(body, dict) and len(json.dumps(body).encode()) <= 64 * 1024 for body in remote
    )
    assert b"directory" not in json.dumps(remote).encode()
    assert b"access-1" not in output.getvalue()
    assert b"device-1" not in output.getvalue()
    uploads = [request for request in tools_service.requests if request[1] == "/direct-upload"]
    assert len(uploads) == 1 and uploads[0][3] == descriptor["upload_authorization"]


def test_private_mcp_denial_remains_a_safe_host_result(
    tools_service: FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """A private-operation refusal cannot stage source or expose the bridge's login credentials."""
    del logged_in
    output = io.BytesIO()

    def frames() -> Iterator[bytes]:
        """Authenticate normally before the private request is refused by the service."""
        yield _initialize()
        tools_service.fail_detail["/mcp"] = (403, "private source permission denied")
        yield _frame(
            "tools/call",
            "private-stage",
            name="private_stage",
            arguments={"service_name": "private"},
        )

    cli._serve_tools_mcp(frames(), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[-1]["id"] == "private-stage"
    assert responses[-1]["error"]["message"] == "private source permission denied"
    assert not any(path == "/direct-upload" for _, path, _, _ in tools_service.requests)
    assert b"access-1" not in output.getvalue()
    assert b"device-1" not in output.getvalue()
