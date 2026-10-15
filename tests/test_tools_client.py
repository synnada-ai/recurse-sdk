"""Public tools consumption and publication through real HTTP boundaries."""

import base64
import hashlib
import json
import json.scanner
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

import recurse
from tests.test_tools_bundle import write_tools_service


class ToolsService:
    """A local HTTP service recording real client requests and durable identities."""

    def __init__(self) -> None:
        """Start a loopback server with configurable response boundaries."""
        self.requests: list[tuple[str, str, str | None, bytes]] = []
        self.headers: list[dict[str, str]] = []
        self.responses: dict[tuple[str, str], tuple[int, bytes, dict[str, str]]] = {}
        self.version_statuses = ["pending", "prepared"]
        self.source: bytes | None = None
        self.lose_upload_ack = False
        self.upload_ack_status: int | None = None
        service = self

        class Handler(BaseHTTPRequestHandler):
            """Respond over HTTP without external data or customer credentials."""

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                """Keep local request logs out of the test output."""

            def respond(self) -> None:
                """Record the boundary request and emit its selected response."""
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                service.requests.append(
                    (self.command, self.path, self.headers.get("Authorization"), body)
                )
                service.headers.append(dict(self.headers))
                status, payload, headers = service.route(self.command, self.path, body)
                if self.path == "/storage" and service.lose_upload_ack:
                    self.close_connection = True
                    return
                if self.path == "/storage" and service.upload_ack_status is not None:
                    status = service.upload_ack_status
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = respond  # noqa: N815 - standard-library HTTP callback name
            do_POST = respond  # noqa: N815 - standard-library HTTP callback name

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def route(self, method: str, path: str, body: bytes) -> tuple[int, bytes, dict[str, str]]:
        """Return configured errors or representative catalog/call/publication results."""
        override = self.responses.get((method, path))
        if override is not None:
            return override
        value: dict[str, Any]
        if method == "POST" and path == "/v1/tool-versions":
            value = {
                "version_id": "version-1",
                "status": "staged",
                "source_upload": {
                    "upload_id": "upload-1",
                    "upload_url": self.url + "/storage",
                    "upload_authorization": "Bearer storage-capability",
                    "upload_content_type": "application/octet-stream",
                    "upload_prefix": base64.b64encode(b"prefix:").decode(),
                    "upload_suffix": base64.b64encode(b":suffix").decode(),
                },
            }
        elif path == "/storage":
            self.source = body.removeprefix(b"prefix:").removesuffix(b":suffix")
            return 201, b"", {}
        elif path == "/v1/tool-versions/version-1":
            status = self.version_statuses[0]
            if len(self.version_statuses) > 1:
                self.version_statuses.pop(0)
            value = {
                "version_id": "version-1",
                "service_name": "fixture-tools",
                "status": status,
                "error": "invalid_source" if status == "failed" else None,
            }
        elif path == "/v1/tool-versions/version-1/publication":
            value = {"version_id": "version-1", "status": "published", "error": None}
        elif path == "/v1/tool-versions/version-1/complete":
            value = {"version_id": "version-1", "status": "pending"}
        elif path.startswith("/v1/catalog?"):
            value = {"tools": [{"id": "fixture-tools.echo"}], "next_cursor": "fixture-tools.echo"}
        elif path.startswith("/v1/catalog/"):
            value = {
                "id": "fixture-tools.echo",
                "version": "tool-v1",
                "price_version": "price-v1",
                "pricing": {"maximum_total_micro_usd": 94_068},
            }
        else:
            value = {
                "call_id": "call-1",
                "status": "pending",
                "billing_status": "reserved",
                "reserved_micro_usd": 94_068,
                "result": None,
            }
        return 200, json.dumps(value).encode(), {"Content-Type": "application/json"}


@pytest.fixture
def tools_service() -> Iterator[ToolsService]:
    """Provide an isolated service and close its server after each test."""
    service = ToolsService()
    try:
        yield service
    finally:
        service.server.shutdown()
        service.server.server_close()
        service.thread.join()


def call(client: Any, inputs: object | None = None) -> dict[str, Any]:
    """Invoke an inspected fixture using explicit bounded admission fields."""
    return cast(
        "dict[str, Any]",
        client.call(
            "fixture-tools.echo",
            tool_version="tool-v1",
            price_version="price-v1",
            inputs={"message": "hello"} if inputs is None else inputs,
            idempotency_key="stable-key",
            max_cost_micro_usd=100_000,
        ),
    )


def test_public_client_routes_and_receipts_do_not_resubmit_reads(
    tools_service: ToolsService,
) -> None:
    """SDK consumption retains inspected versions, cap, key and raw durable receipts."""
    assert hasattr(recurse, "ToolsClient"), "missing public tools client"
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    page = client.catalog_search("echo & more", limit=1, cursor="opaque/+&?")
    assert page == {"tools": [{"id": "fixture-tools.echo"}], "next_cursor": "fixture-tools.echo"}
    query = parse_qs(urlsplit(tools_service.requests[-1][1]).query)
    assert query == {"search": ["echo & more"], "limit": ["1"], "cursor": ["opaque/+&?"]}
    assert client.catalog_get("fixture tools/echo")["version"] == "tool-v1"
    receipt = call(client)
    assert receipt["billing_status"] == "reserved"
    assert client.get_call("call-1") == receipt
    assert client.get_call("call-1") == receipt
    assert client.cancel_call("call-1") == receipt
    requests = tools_service.requests
    assert requests[1][1] == "/v1/catalog/fixture%20tools%2Fecho"
    assert [path for method, path, _token, _body in requests if method == "POST"] == [
        "/v1/calls",
        "/v1/calls/call-1/cancel",
    ]
    assert json.loads(requests[2][3]) == {
        "tool_id": "fixture-tools.echo",
        "tool_version": "tool-v1",
        "price_version": "price-v1",
        "inputs": {"message": "hello"},
        "idempotency_key": "stable-key",
        "max_cost_micro_usd": 100_000,
    }
    assert all(bearer == "Bearer tools-token" for _method, _path, bearer, _body in requests)
    assert "Content-Type" not in tools_service.headers[0]


def test_default_catalog_and_version_resume_routes(tools_service: ToolsService) -> None:
    """Default paging has no cursor and publication can resume a known version."""
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url + "/")
    client.catalog_search()
    client.get_version("version-1")
    result = client.publish_version("version-1", {"tools": {}})
    assert result["status"] == "published"
    assert tools_service.requests[0][1] == "/v1/catalog?search=&limit=20"
    assert json.loads(tools_service.requests[-1][3]) == {"tools": {}}


def test_api_catalog_uses_tools_authority_and_returns_metadata_unchanged(
    tools_service: ToolsService,
) -> None:
    """Discovery exposes approved endpoint contracts and limits without submitting paid calls."""
    listing = {"items": [{"id": "exa.web.search", "version": "api-v1"}], "cursor": None}
    definition = {
        "id": "exa.web.search",
        "version": "api-v1",
        "input_schema": {"type": "object"},
        "max_cost_micro_usd": 7000,
        "pricing": {"provider_multiplier": 1, "compute_multiplier": 4},
    }
    tools_service.responses[("GET", "/v1/apis?query=search+%26+extract&limit=2")] = (
        200,
        json.dumps(listing).encode(),
        {},
    )
    tools_service.responses[("GET", "/v1/apis/provider%20%2Fendpoint")] = (
        200,
        json.dumps(definition).encode(),
        {},
    )
    client = recurse.ToolsClient("customer-token", base_url=tools_service.url)
    assert client.apis_search("search & extract", limit=2) == listing
    assert client.apis_get("provider /endpoint") == definition
    client.apis_search()
    assert tools_service.requests[-1][1] == "/v1/apis?query=&limit=20"
    assert all(
        method == "GET" and body == b"" for method, _path, _token, body in tools_service.requests
    )
    assert all(
        bearer == "Bearer customer-token"
        for _method, _path, bearer, _body in tools_service.requests
    )


@pytest.mark.parametrize("token", ["", " ", "bad\r\nAuthorization: injected", None, 123])
def test_missing_or_invalid_bearer_is_a_typed_error(token: Any) -> None:
    """A client cannot construct invalid bearer headers or infer server credentials."""
    with pytest.raises(recurse.ToolsError, match="access token"):
        recurse.ToolsClient(token)


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://tools.example",
        "https://",
        "https://user:secret@tools.example",
        "https://tools.example/mcp",
        "https://tools.example?q=x",
        "https://tools.example#x",
        "https://tools.example\n",
    ],
)
def test_invalid_origin_is_rejected_before_network(origin: str) -> None:
    """Tokens are sent only to a valid explicit HTTP origin."""
    with pytest.raises(recurse.ToolsError, match="base URL"):
        recurse.ToolsClient("token", base_url=origin)


def test_environment_origin_and_static_auth_bypass_local_login(
    tools_service: ToolsService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server callers choose an origin without using an operating-system login."""
    monkeypatch.setenv("RECURSE_TOOLS_URL", tools_service.url)
    monkeypatch.setitem(sys.modules, "_recurse_cli", None)
    assert recurse.ToolsClient("tools-token").catalog_search()["tools"]


@pytest.mark.parametrize(
    "inputs",
    [{"x": float("nan")}, {"x": float("inf")}, {1: "x"}, [1], {"x": {1, 2}}, {"x": "\ud800"}],
)
def test_non_finite_or_non_mapping_inputs_fail_before_admission(
    tools_service: ToolsService, inputs: object
) -> None:
    """Inputs cannot change meaning or escape ordinary JSON during serialization."""
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="JSON"):
        call(client, inputs)
    assert tools_service.requests == []


@pytest.mark.parametrize("status", [401, 403, 409, 422, 502])
def test_http_errors_are_typed_and_have_no_transport_retry(
    tools_service: ToolsService, status: int
) -> None:
    """Rejected calls preserve HTTP status without retrying accepted provider work."""
    tools_service.responses[("POST", "/v1/calls")] = (
        status,
        b'{"detail":"call rejected"}',
        {},
    )
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        call(client)
    assert failure.value.status_code == status
    assert len(tools_service.requests) == 1
    if status >= 500:
        assert "call rejected" not in str(failure.value)


@pytest.mark.parametrize("payload", [b"[]", b"null", b"broken", b'"str"', b'{"n":NaN}', b"\xff"])
def test_malformed_success_is_a_typed_error(tools_service: ToolsService, payload: bytes) -> None:
    """Only ordinary finite JSON object responses reach SDK consumers."""
    tools_service.responses[("GET", "/v1/calls/call-1")] = (200, payload, {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="invalid response"):
        client.get_call("call-1")


def test_bearer_redirect_is_not_followed(tools_service: ToolsService) -> None:
    """A redirect cannot forward a customer bearer to a new endpoint."""
    tools_service.responses[("GET", "/v1/calls/call-1")] = (
        302,
        b"",
        {"Location": tools_service.url + "/leaked"},
    )
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.get_call("call-1")
    assert failure.value.status_code == 302
    assert len(tools_service.requests) == 1


def test_unreachable_origin_has_safe_error_without_token() -> None:
    """Connection failures never include bearer or private request contents."""
    client = recurse.ToolsClient("private-token", base_url="http://127.0.0.1:1")
    with pytest.raises(recurse.ToolsError) as failure:
        call(client, {"private": "sensitive"})
    assert "private-token" not in str(failure.value)
    assert "sensitive" not in str(failure.value)


def test_publication_builds_real_bundle_and_uses_storage_capability(
    tools_service: ToolsService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build/upload/prepare/publish follows one version and keeps resource bearer off Storage."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    directory = write_tools_service(tmp_path / "service")
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    result = client.publish(directory)
    assert result["status"] == "published"
    assert tools_service.source is not None
    stage = json.loads(tools_service.requests[0][3])
    assert stage["service_name"] == "fixture-tools"
    assert stage["application_record"] == {
        "apiVersion": "recurse.tools.application/v1alpha1",
        "source": {
            "sha256": hashlib.sha256(tools_service.source).hexdigest(),
            "size_bytes": len(tools_service.source),
        },
    }
    storage = [entry for entry in tools_service.requests if entry[1] == "/storage"]
    assert len(storage) == 1
    assert storage[0][2] == "Bearer storage-capability"
    complete = [entry for entry in tools_service.requests if entry[1].endswith("/complete")]
    assert json.loads(complete[0][3]) == {"source_upload_id": "upload-1"}
    assert json.loads(tools_service.requests[-1][3]) == {"tools": {}}


def test_publisher_denial_happens_before_upload(
    tools_service: ToolsService, tmp_path: Path
) -> None:
    """A nonpublisher cannot upload or prepare after failed stage authority."""
    tools_service.responses[("POST", "/v1/tool-versions")] = (
        403,
        b'{"detail":"publisher only"}',
        {},
    )
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.publish(write_tools_service(tmp_path / "service"))
    assert failure.value.status_code == 403
    assert failure.value.version_id is None
    assert [request[1] for request in tools_service.requests] == ["/v1/tool-versions"]


@pytest.mark.parametrize("status", ["failed", "cancelled", "timed_out", "infrastructure_failed"])
def test_preparation_failure_retains_version_for_resumption(
    tools_service: ToolsService, tmp_path: Path, status: str
) -> None:
    """A failed prepared version never creates another staging record or publication."""
    tools_service.version_statuses = [status]
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.publish(write_tools_service(tmp_path / "service"))
    assert failure.value.version_id == "version-1"
    assert "version-1" in str(failure.value)
    assert not any(request[1].endswith("/publication") for request in tools_service.requests)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "bad"])
def test_invalid_publication_timeout_has_no_writes(
    tools_service: ToolsService, tmp_path: Path, timeout: Any
) -> None:
    """Invalid time bounds fail before building or allocating a version."""
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="timeout"):
        client.publish(tmp_path / "missing", timeout_seconds=timeout)
    assert tools_service.requests == []


@pytest.mark.parametrize("body", [b"not-json", b"[]", b"{}", b'{"detail":null}', b'{"detail":""}'])
def test_non_public_http_error_body_is_not_exposed(
    tools_service: ToolsService, body: bytes
) -> None:
    """Malformed or empty public error detail uses a safe status-based message."""
    tools_service.responses[("GET", "/v1/calls/call-1")] = (403, body, {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.get_call("call-1")
    assert failure.value.status_code == 403
    assert "HTTP 403" in str(failure.value)


@pytest.mark.parametrize("payload", [json.dumps({"large": "x" * 100}).encode(), b"{}" + b" " * 100])
def test_oversized_service_result_is_bounded(
    tools_service: ToolsService, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    """The HTTP reader rejects a complete object exceeding its response bound."""
    monkeypatch.setattr(recurse, "_TOOLS_MAX_RESPONSE_BYTES", 64)
    tools_service.responses[("GET", "/v1/calls/call-1")] = (
        200,
        payload,
        {},
    )
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="invalid response"):
        client.get_call("call-1")


def test_default_origin_is_public_tools_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing tools override never selects the agent API origin."""
    monkeypatch.delenv("RECURSE_TOOLS_URL", raising=False)
    monkeypatch.setenv("RECURSE_API_URL", "https://api.dev.recurse.run")
    assert recurse.ToolsClient("token").base_url == "https://tools.recurse.run"


@pytest.mark.parametrize(
    "origin",
    [
        "https://tools.example:invalid",
        "https://tools.example/\x01",
        "https://töols.example",
        "https://tools.example\x01",
        5,
    ],
)
def test_unsafe_or_malformed_origin_cannot_send_bearers(origin: Any) -> None:
    """Port syntax, control characters and ambiguous origins fail before authentication."""
    with pytest.raises(recurse.ToolsError, match="base URL"):
        recurse.ToolsClient("token", base_url=origin)


def stage_response(service: ToolsService) -> dict[str, Any]:
    """Copy the representative stage response for malformed capability tests."""
    return cast("dict[str, Any]", json.loads(service.route("POST", "/v1/tool-versions", b"")[1]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_upload", None),
        ("upload_url", None),
        ("upload_url", "ftp://storage.example/file"),
        ("upload_url", "https://"),
        ("upload_url", "https://user:secret@storage.example/file"),
        ("upload_prefix", "not-base64"),
        ("upload_suffix", "not-base64"),
        ("upload_authorization", None),
        ("upload_authorization", "Bearer capability\nInjected: header"),
        ("upload_content_type", None),
        ("upload_id", None),
    ],
)
def test_malformed_upload_descriptor_preserves_allocated_version(
    tools_service: ToolsService, tmp_path: Path, field: str, value: object
) -> None:
    """An invalid capability cannot authorize another upload or silently restage."""
    staged = stage_response(tools_service)
    if field == "source_upload":
        staged[field] = value
    else:
        staged["source_upload"][field] = value
    tools_service.responses[("POST", "/v1/tool-versions")] = (200, json.dumps(staged).encode(), {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.publish(write_tools_service(tmp_path / "service"))
    assert failure.value.version_id == "version-1"
    assert (
        sum(path == "/v1/tool-versions" for _method, path, _token, _body in tools_service.requests)
        == 1
    )


def test_rejected_upload_is_not_retried_or_restaged(
    tools_service: ToolsService, tmp_path: Path
) -> None:
    """Failed direct transfer keeps its original source version without repeated writes."""
    tools_service.responses[("POST", "/storage")] = (403, b"private response", {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.publish(write_tools_service(tmp_path / "service"))
    assert failure.value.version_id == "version-1"
    assert "private response" not in str(failure.value)
    assert [path for _method, path, _token, _body in tools_service.requests] == [
        "/v1/tool-versions",
        "/storage",
    ]


def test_upload_redirect_does_not_forward_storage_capability(
    tools_service: ToolsService, tmp_path: Path
) -> None:
    """Opaque upload authority cannot move to a redirected destination."""
    tools_service.responses[("POST", "/storage")] = (
        307,
        b"",
        {"Location": tools_service.url + "/leaked"},
    )
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError):
        client.publish(write_tools_service(tmp_path / "service"))
    assert [path for _method, path, _token, _body in tools_service.requests] == [
        "/v1/tool-versions",
        "/storage",
    ]


def test_timeout_retains_same_preparation_version(
    tools_service: ToolsService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Polling expiry returns a resumable version and no second preparation admission."""
    tools_service.version_statuses = ["running"]
    times = iter([10.0, 12.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(times))
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="timeout") as failure:
        client.publish(write_tools_service(tmp_path / "service"), timeout_seconds=1)
    assert failure.value.version_id == "version-1"
    assert (
        sum(path.endswith("/complete") for _method, path, _token, _body in tools_service.requests)
        == 1
    )
    assert not any(
        path.endswith("/publication") for _method, path, _token, _body in tools_service.requests
    )


def test_paid_publication_config_is_retained_on_resume(
    tools_service: ToolsService, tmp_path: Path
) -> None:
    """Publisher policy stays separate from authored schema and survives preparation."""
    tools_service.version_statuses = ["published"]
    config = {"tools": {"echo": {"billing": {"provider_rate_version": "provider-none-v1"}}}}
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    result = client.publish(write_tools_service(tmp_path / "service"), publication_config=config)
    assert result["status"] == "published"
    assert json.loads(tools_service.requests[-1][3]) == config
    assert tools_service.headers[1]["User-Agent"] == "recurse-sdk"


@pytest.mark.parametrize("config", [[], {"tools": {"x": float("nan")}}])
def test_invalid_publication_json_fails_before_staging(
    tools_service: ToolsService, tmp_path: Path, config: Any
) -> None:
    """Publisher configuration must be finite ordinary JSON before any external writes."""
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="JSON"):
        client.publish(tmp_path / "missing", publication_config=config)
    assert tools_service.requests == []


@pytest.mark.parametrize("method", ["tools/list", "tools/call"])
def test_mcp_transport_adds_required_protocol_headers(
    tools_service: ToolsService, method: str
) -> None:
    """Modern stateless MCP transport retains method/name headers without Tasks."""
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}
    if method == "tools/call":
        body["params"] = {"name": "catalog_search", "arguments": {}}
    client._request("POST", "/mcp", body)
    headers = {name.lower(): value for name, value in tools_service.headers[-1].items()}
    assert headers["mcp-protocol-version"] == "2026-07-28"
    assert headers["mcp-method"] == method
    if method == "tools/call":
        assert headers["mcp-name"] == "catalog_search"
    else:
        assert "mcp-name" not in headers


from tests import test_cli  # noqa: E402 - reused real issuer/keychain test fixtures

service = test_cli.service
keychain = test_cli.keychain
logged_in = test_cli.logged_in
_ephemeral_callback_port = test_cli._ephemeral_callback_port


def test_saved_login_refresh_replays_exact_call_once(
    tools_service: ToolsService,
    service: test_cli.FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected local bearer refreshes the exact Tools resource without a new retry key."""
    service.token_responses = ["access-old", "access-new"]
    route = tools_service.route

    def reject_old(method: str, path: str, body: bytes) -> tuple[int, bytes, dict[str, str]]:
        """Reject authentication before admitting a request using the new bearer."""
        if tools_service.requests[-1][2] == "Bearer access-old":
            return 401, b'{"detail":"expired"}', {}
        return route(method, path, body)

    monkeypatch.setattr(tools_service, "route", reject_old)
    client = recurse.ToolsClient.from_login(base_url=tools_service.url)
    assert call(client)["call_id"] == "call-1"
    assert len(tools_service.requests) == 2
    assert tools_service.requests[0][3] == tools_service.requests[1][3]
    assert [request[2] for request in tools_service.requests] == [
        "Bearer access-old",
        "Bearer access-new",
    ]
    grants = [
        body for _method, path, body, _bearer in service.requests if path == "/v1/oauth/token"
    ]
    assert grants == [
        {
            "grant_type": "refresh_token",
            "client_id": "recurse-cli",
            "refresh_token": "device-1",
            "resource": tools_service.url + "/mcp",
        },
        {
            "grant_type": "refresh_token",
            "client_id": "recurse-cli",
            "refresh_token": "device-1",
            "resource": tools_service.url + "/mcp",
        },
    ]
    cached = json.loads(logged_in[("recurse-cli", "tools-access-token:" + service.url)])
    assert cached["resource"] == tools_service.url + "/mcp"


def test_saved_login_refresh_is_bounded_after_second_rejection(
    tools_service: ToolsService,
    service: test_cli.FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Two rejected resource bearers produce one typed failure rather than a refresh loop."""
    tools_service.responses[("POST", "/v1/calls")] = (401, b"{}", {})
    client = recurse.ToolsClient.from_login(base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        call(client)
    assert failure.value.status_code == 401
    assert len(tools_service.requests) == 2
    assert service.device_grants == ["device-1", "device-1"]
    assert logged_in


def test_local_factory_requires_login_without_opening_browser(
    tools_service: ToolsService,
    service: test_cli.FakeService,
    keychain: dict[tuple[str, str], str],
) -> None:
    """Missing local credentials fail at the explicit factory without network admission."""
    with pytest.raises(recurse.ToolsError, match="recurse login"):
        recurse.ToolsClient.from_login(base_url=tools_service.url)
    assert tools_service.requests == []
    assert service.requests == []
    assert not keychain


def test_failed_resource_refresh_is_safe_and_typed(
    tools_service: ToolsService,
    service: test_cli.FakeService,
    logged_in: dict[tuple[str, str], str],
) -> None:
    """Failed issuer responses do not leak private error contents through the public SDK."""
    service.fail_detail["/v1/oauth/token"] = (503, "private refresh details")
    with pytest.raises(recurse.ToolsError) as failure:
        recurse.ToolsClient.from_login(base_url=tools_service.url)
    assert failure.value.status_code == 503
    assert "private refresh details" not in str(failure.value)
    assert tools_service.requests == []
    assert logged_in


def test_failed_call_receipt_is_returned_as_data(tools_service: ToolsService) -> None:
    """Known execution failure and provider charges remain inspectable receipt data."""
    receipt = {
        "call_id": "call-1",
        "status": "failed",
        "billing_status": "settled",
        "charge_micro_usd": 4000,
        "error": "invalid_output",
        "result": None,
    }
    tools_service.responses[("GET", "/v1/calls/call-1")] = (200, json.dumps(receipt).encode(), {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    assert client.get_call("call-1") == receipt


def test_lost_upload_ack_uses_completion_authority_without_resending_source(
    tools_service: ToolsService, tmp_path: Path
) -> None:
    """Stored source survives a lost HTTP acknowledgment through one completion admission."""
    tools_service.lose_upload_ack = True
    tools_service.version_statuses = ["prepared"]
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    result = client.publish(write_tools_service(tmp_path / "service"))
    assert result["status"] == "published"
    assert tools_service.source is not None
    paths = [path for _method, path, _token, _body in tools_service.requests]
    assert paths == [
        "/v1/tool-versions",
        "/storage",
        "/v1/tool-versions/version-1/complete",
        "/v1/tool-versions/version-1",
        "/v1/tool-versions/version-1/publication",
    ]


def test_mcp_method_header_cannot_be_built_from_a_non_string(
    tools_service: ToolsService,
) -> None:
    """Malformed RPC data reaches validation without an invalid HTTP method header."""
    tools_service.responses[("POST", "/mcp")] = (400, b'{"detail":"invalid MCP request"}', {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client._request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": 1})
    assert failure.value.status_code == 400
    assert "mcp-method" not in {name.lower() for name in tools_service.headers[-1]}


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_stored_upload_with_gateway_error_uses_completion_without_resending(
    tools_service: ToolsService, tmp_path: Path, status: int
) -> None:
    """A gateway failure after durable storage is resolved by the same version's completion."""
    tools_service.upload_ack_status = status
    tools_service.version_statuses = ["prepared"]
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    result = client.publish(write_tools_service(tmp_path / "service"))
    assert result["status"] == "published"
    assert tools_service.source is not None
    paths = [path for _method, path, _token, _body in tools_service.requests]
    assert paths == [
        "/v1/tool-versions",
        "/storage",
        "/v1/tool-versions/version-1/complete",
        "/v1/tool-versions/version-1",
        "/v1/tool-versions/version-1/publication",
    ]


def test_gateway_upload_failure_without_source_keeps_completion_rejection(
    tools_service: ToolsService, tmp_path: Path
) -> None:
    """Missing source closes with authoritative rejection and the original version identity."""
    tools_service.responses[("POST", "/storage")] = (503, b"private gateway error", {})
    tools_service.responses[("POST", "/v1/tool-versions/version-1/complete")] = (
        422,
        b'{"detail":"source is missing"}',
        {},
    )
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.publish(write_tools_service(tmp_path / "service"))
    assert failure.value.version_id == "version-1"
    assert failure.value.status_code == 422
    assert "source is missing" in str(failure.value)
    assert "private gateway error" not in str(failure.value)
    assert tools_service.source is None
    assert [path for _method, path, _token, _body in tools_service.requests] == [
        "/v1/tool-versions",
        "/storage",
        "/v1/tool-versions/version-1/complete",
    ]


def test_deep_request_json_fails_typed_before_admission(tools_service: ToolsService) -> None:
    """Nested caller data cannot escape the public JSON error boundary through recursion."""
    inputs: dict[str, Any] = {}
    nested = inputs
    for _level in range(1500):
        child: dict[str, Any] = {}
        nested["nested"] = child
        nested = child
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="JSON"):
        call(client, inputs)
    assert tools_service.requests == []


@pytest.mark.parametrize("depth", [700, 1500])
def test_deep_success_json_is_a_safe_typed_failure(tools_service: ToolsService, depth: int) -> None:
    """Decoder and recursive portability exhaustion both fail as public response errors."""
    payload = b'{"nested":' + b"[" * depth + b"null" + b"]" * depth + b"}"
    tools_service.responses[("GET", "/v1/calls/call-1")] = (200, payload, {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError, match="invalid response"):
        client.get_call("call-1")
    assert len(tools_service.requests) == 1


@pytest.mark.parametrize("decoder", ["native", "python"])
def test_deep_error_json_retains_public_http_failure(
    tools_service: ToolsService, monkeypatch: pytest.MonkeyPatch, decoder: str
) -> None:
    """Unparseable deeply nested error data preserves status without leaking a traceback."""
    if decoder == "python":
        # Exercise the stdlib decoder used when the optional C accelerator is absent.
        monkeypatch.setattr(json.scanner, "make_scanner", cast("Any", json.scanner).py_make_scanner)
        monkeypatch.setattr(json, "_default_decoder", json.JSONDecoder())
    payload = b'{"detail":' + b"[" * 1500 + b"null" + b"]" * 1500 + b"}"
    tools_service.responses[("GET", "/v1/calls/call-1")] = (403, payload, {})
    client = recurse.ToolsClient("tools-token", base_url=tools_service.url)
    with pytest.raises(recurse.ToolsError) as failure:
        client.get_call("call-1")
    assert failure.value.status_code == 403
    assert "HTTP 403" in str(failure.value)
