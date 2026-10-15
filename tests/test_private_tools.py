"""Private project preparation and execution use real bounded HTTP boundaries."""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import recurse
from tests.test_tools_bundle import write_tools_service
from tests.test_tools_client import ToolsService


class PrivateToolsService(ToolsService):
    """Model content-addressed private preparation and durable call admission."""

    def __init__(self) -> None:
        """Record one owner's source and immutable call request identities."""
        self.registered = False
        self.calls: dict[str, dict[str, Any]] = {}
        self.replay_candidates: dict[str, dict[str, Any]] = {}
        self.admitted_semantics: dict[str, dict[str, Any]] = {}
        super().__init__()

    def route(  # noqa: PLR0911 - explicit private HTTP lifecycle and replay responses
        self, method: str, path: str, body: bytes
    ) -> tuple[int, bytes, dict[str, str]]:
        """Reuse source versions and reject changed requests under an existing key."""
        if (method, path) in self.responses:
            return self.responses[(method, path)]
        if path == "/v1/private-calls/replay":
            value = json.loads(body)
            key = value["idempotency_key"]
            if key in self.calls:
                if self.admitted_semantics[key] != value:
                    return 409, b'{"detail":"idempotency key conflict"}', {}
                receipt = json.loads(super().route("GET", "/v1/calls/call-1", b"")[1])
                return 200, json.dumps({"receipt": receipt}).encode(), {}
            self.replay_candidates[key] = value
            return 200, b'{"receipt":null}', {}
        if path == "/v1/private-tool-versions":
            response = super().route("POST", "/v1/tool-versions", body)
            value = json.loads(response[1])
            if self.registered:
                value = {"version_id": "version-1", "status": "prepared"}
            self.registered = True
            return 200, json.dumps(value).encode(), {}
        if path.startswith("/v1/private-tool-versions/"):
            if "/tools/" in path:
                return super().route("GET", "/v1/catalog/fixture-tools.echo", body)
            return super().route(
                method, path.replace("/private-tool-versions/", "/tool-versions/"), body
            )
        if path == "/v1/private-calls":
            value = json.loads(body)
            key = value["idempotency_key"]
            if key in self.calls and self.calls[key] != value:
                return 409, b'{"detail":"idempotency key conflict"}', {}
            self.calls[key] = value
            if key in self.replay_candidates:
                self.admitted_semantics[key] = self.replay_candidates[key]
        return super().route(method, path, body)


@pytest.fixture
def private_service() -> Iterator[PrivateToolsService]:
    """Provide an isolated private service and close its HTTP listener."""
    service = PrivateToolsService()
    try:
        yield service
    finally:
        service.server.shutdown()
        service.server.server_close()
        service.thread.join()


def private_call(client: recurse.ToolsClient, inputs: Any = None) -> dict[str, Any]:
    """Submit an explicitly inspected version with the same stable request key."""
    return client.call_private(
        "version /one",
        "echo /name",
        tool_version="tool-v1",
        price_version="price-v1",
        inputs={"message": "hello"} if inputs is None else inputs,
        idempotency_key="private-key",
        max_cost_micro_usd=200_000,
    )


def test_private_run_builds_prepares_inspects_and_admits_without_publication(
    private_service: PrivateToolsService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ordinary customer's project uses private routes and existing durable call receipts."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    directory = write_tools_service(tmp_path / "project")
    client = recurse.ToolsClient("customer-token", base_url=private_service.url)
    first = client.run(
        directory,
        "echo",
        inputs={"message": "hello"},
        idempotency_key="same-request",
        max_cost_micro_usd=200_000,
    )
    second = client.run(
        directory,
        "echo",
        inputs={"message": "hello"},
        idempotency_key="same-request",
        max_cost_micro_usd=200_000,
    )
    assert first == second
    assert first["call_id"] == "call-1"
    assert len(private_service.calls) == 1
    paths = [path for _method, path, _token, _body in private_service.requests]
    assert paths.count("/storage") == 1
    assert paths.count("/v1/private-tool-versions/version-1/complete") == 1
    assert "/v1/private-tool-versions/version-1/tools/echo" in paths
    assert not any("/publication" in path or path == "/v1/tool-versions" for path in paths)
    registrations = [
        json.loads(body)
        for _, path, _, body in private_service.requests
        if path == "/v1/private-tool-versions"
    ]
    assert len(registrations) == 1
    assert registrations[0]["max_cost_micro_usd"] == 200_000
    assert (
        registrations[0]["application_record"]["apiVersion"] == "recurse.tools.application/v1alpha1"
    )
    assert private_service.calls["same-request"] == {
        "version_id": "version-1",
        "function_name": "echo",
        "tool_version": "tool-v1",
        "price_version": "price-v1",
        "inputs": {"message": "hello"},
        "idempotency_key": "same-request",
        "max_cost_micro_usd": 200_000,
    }
    assert client.get_call(first["call_id"])["billing_status"] == "reserved"
    assert client.cancel_call(first["call_id"])["call_id"] == first["call_id"]


def test_private_preparation_reuses_stored_source_version(
    private_service: PrivateToolsService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated preparation skips another upload or preparation dispatch for identical source."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    directory = write_tools_service(tmp_path / "project")
    client = recurse.ToolsClient("token", base_url=private_service.url)
    first = client.prepare_private(directory, max_cost_micro_usd=200_000)
    second = client.prepare_private(directory, max_cost_micro_usd=200_000)
    assert first == second
    paths = [path for _method, path, _token, _body in private_service.requests]
    assert paths.count("/v1/private-tool-versions") == 2
    assert paths.count("/storage") == 1
    assert paths.count("/v1/private-tool-versions/version-1/complete") == 1


def test_run_replays_original_call_after_source_runtime_retirement(
    private_service: PrivateToolsService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exact retry recovers the original receipt before charging another preparation."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    directory = write_tools_service(tmp_path / "project")
    client = recurse.ToolsClient("token", base_url=private_service.url)
    first = client.run(
        directory,
        "echo",
        inputs={"message": "hello"},
        idempotency_key="same-request",
        max_cost_micro_usd=200_000,
    )
    request_count = len(private_service.requests)
    private_service.registered = False
    private_service.version_statuses = ["retired"]
    second = client.run(
        directory,
        "echo",
        inputs={"message": "hello"},
        idempotency_key="same-request",
        max_cost_micro_usd=200_000,
    )
    assert first == second
    assert [path for _, path, _, _ in private_service.requests[request_count:]] == [
        "/v1/private-calls/replay"
    ]


@pytest.mark.parametrize("change", ["source", "inputs", "cap"])
def test_run_conflicts_are_rejected_before_another_paid_preparation(
    private_service: PrivateToolsService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """An existing key cannot fund preparation of changed source, inputs or monetary authority."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    directory = write_tools_service(tmp_path / "project")
    client = recurse.ToolsClient("token", base_url=private_service.url)
    client.run(
        directory,
        "echo",
        inputs={"message": "hello"},
        idempotency_key="same-request",
        max_cost_micro_usd=200_000,
    )
    if change == "source":
        with (directory / "tools.py").open("a") as source:
            source.write("\n# Changed source version.\n")
    request_count = len(private_service.requests)
    with pytest.raises(recurse.ToolsError, match="idempotency key conflict"):
        client.run(
            directory,
            "echo",
            inputs={"message": "changed" if change == "inputs" else "hello"},
            idempotency_key="same-request",
            max_cost_micro_usd=300_000 if change == "cap" else 200_000,
        )
    assert [path for _, path, _, _ in private_service.requests[request_count:]] == [
        "/v1/private-calls/replay"
    ]


@pytest.mark.parametrize("payload", [{}, {"receipt": []}, {"receipt": "invalid"}])
def test_run_rejects_unknown_replay_response_before_preparation(
    private_service: PrivateToolsService, tmp_path: Path, payload: dict[str, Any]
) -> None:
    """A malformed replay response cannot authorize a new paid preparation."""
    private_service.responses[("POST", "/v1/private-calls/replay")] = (
        200,
        json.dumps(payload).encode(),
        {},
    )
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="invalid private replay"):
        client.run(
            write_tools_service(tmp_path / "project"),
            "echo",
            inputs={"message": "hello"},
            idempotency_key="same-request",
            max_cost_micro_usd=200_000,
        )
    assert [path for _, path, _, _ in private_service.requests] == ["/v1/private-calls/replay"]


def test_private_inspection_quotes_each_path_component(
    private_service: PrivateToolsService,
) -> None:
    """Identifiers cannot change private ownership or function route boundaries."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    client.inspect_private("version /one", "echo /name")
    assert (
        private_service.requests[-1][1]
        == "/v1/private-tool-versions/version%20%2Fone/tools/echo%20%2Fname"
    )
    receipt = private_call(client)
    assert receipt["call_id"] == "call-1"
    assert private_service.calls["private-key"]["function_name"] == "echo /name"


def test_changed_private_inputs_cannot_reuse_an_admitted_key(
    private_service: PrivateToolsService,
) -> None:
    """An exact retry is recoverable but changed arguments receive a conflict."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    assert private_call(client) == private_call(client)
    with pytest.raises(recurse.ToolsError, match="idempotency key conflict") as failure:
        private_call(client, {"message": "changed"})
    assert failure.value.status_code == 409
    assert len(private_service.calls) == 1


@pytest.mark.parametrize("inputs", [[], {"value": float("nan")}])
def test_invalid_private_inputs_do_not_admit(
    private_service: PrivateToolsService, inputs: Any
) -> None:
    """Invalid objects are rejected locally before any private admission."""
    with pytest.raises(recurse.ToolsError, match="JSON"):
        private_call(recurse.ToolsClient("token", base_url=private_service.url), inputs)
    assert private_service.requests == []


def test_run_validates_inputs_before_preparation(private_service: PrivateToolsService) -> None:
    """Invalid input cannot allocate source storage or a preparation run."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="JSON"):
        client.run(
            "missing",
            "echo",
            inputs={"n": float("nan")},
            idempotency_key="key",
            max_cost_micro_usd=1,
        )
    assert private_service.requests == []


@pytest.mark.parametrize("function_name", ["missing", [], 1])
def test_run_rejects_unknown_function_before_paid_preparation(
    private_service: PrivateToolsService, tmp_path: Path, function_name: Any
) -> None:
    """An invalid function name cannot incur preparation charges or allocate source storage."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="not registered"):
        client.run(
            write_tools_service(tmp_path / "project"),
            function_name,
            inputs={"message": "hello"},
            idempotency_key="private-key",
            max_cost_micro_usd=200_000,
        )
    assert private_service.requests == []


@pytest.mark.parametrize("inputs", [{}, {"message": 1}])
def test_run_checks_input_schema_before_paid_preparation(
    private_service: PrivateToolsService, tmp_path: Path, inputs: dict[str, Any]
) -> None:
    """Schema failures are local and cannot trigger paid source preparation."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="function schema"):
        client.run(
            write_tools_service(tmp_path / "project"),
            "echo",
            inputs=inputs,
            idempotency_key="private-key",
            max_cost_micro_usd=200_000,
        )
    assert private_service.requests == []


@pytest.mark.parametrize("upload_failure", ["transport", 500, 503])
def test_private_upload_unknown_acknowledgment_checks_completion_once(
    private_service: PrivateToolsService, tmp_path: Path, upload_failure: str | int
) -> None:
    """A stored source with a lost acknowledgment is completed without source retransmission."""
    if isinstance(upload_failure, str):
        private_service.lose_upload_ack = True
    else:
        private_service.upload_ack_status = upload_failure
    private_service.version_statuses = ["prepared"]
    client = recurse.ToolsClient("token", base_url=private_service.url)
    assert (
        client.prepare_private(
            write_tools_service(tmp_path / "project"), max_cost_micro_usd=200_000
        )["status"]
        == "prepared"
    )
    paths = [path for _method, path, _token, _body in private_service.requests]
    assert paths.count("/storage") == 1
    assert paths.count("/v1/private-tool-versions/version-1/complete") == 1
    assert paths.count("/v1/private-tool-versions") == 1


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "bad"])
def test_private_timeout_is_validated_before_building(
    private_service: PrivateToolsService, timeout: Any
) -> None:
    """Preparation requires a positive finite wait before source work begins."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="timeout"):
        client.prepare_private("missing", max_cost_micro_usd=200_000, timeout_seconds=timeout)
    assert private_service.requests == []


@pytest.mark.parametrize("cap", [True, -1, 1.5, "200000"])
def test_invalid_private_cost_cap_is_rejected_before_building(
    private_service: PrivateToolsService, cap: Any
) -> None:
    """Invalid monetary units cannot authorize preparation or consume storage."""
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="maximum tools cost"):
        client.prepare_private("missing", max_cost_micro_usd=cap)
    assert private_service.requests == []


def test_zero_cap_reaches_service_quote_validation(
    private_service: PrivateToolsService, tmp_path: Path
) -> None:
    """A valid zero boundary is sent unchanged and cannot fund a positive preparation quote."""
    private_service.responses[("POST", "/v1/private-tool-versions")] = (
        422,
        b'{"detail":"cap below preparation quote"}',
        {},
    )
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="below preparation quote") as failure:
        client.prepare_private(write_tools_service(tmp_path / "project"), max_cost_micro_usd=0)
    assert failure.value.status_code == 422
    assert failure.value.version_id is None
    assert json.loads(private_service.requests[0][3])["max_cost_micro_usd"] == 0


@pytest.mark.parametrize(
    "status", ["failed", "cancelled", "timed_out", "infrastructure_failed", "retired"]
)
def test_private_preparation_failure_retains_source_identity(
    private_service: PrivateToolsService, tmp_path: Path, status: str
) -> None:
    """Preparation failure remains inspectable and cannot fall through to admission."""
    private_service.version_statuses = [status]
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match=status) as failure:
        client.prepare_private(
            write_tools_service(tmp_path / "project"), max_cost_micro_usd=200_000
        )
    assert failure.value.version_id == "version-1"
    assert "get_private_version" in str(failure.value)
    assert private_service.calls == {}


def test_private_preparation_timeout_retains_source_identity(
    private_service: PrivateToolsService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local wait timeout never creates another version or repeats admission."""
    private_service.version_statuses = ["running"]
    clocks = iter([0.0, 2.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clocks))
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="before the timeout") as failure:
        client.prepare_private(
            write_tools_service(tmp_path / "project"), max_cost_micro_usd=200_000, timeout_seconds=1
        )
    assert failure.value.version_id == "version-1"


def test_invalid_private_upload_preserves_source_identity(
    private_service: PrivateToolsService, tmp_path: Path
) -> None:
    """Missing source capability fails without losing the already allocated private version."""
    private_service.responses[("POST", "/v1/private-tool-versions")] = (
        200,
        b'{"version_id":"version-1","status":"staged"}',
        {},
    )
    client = recurse.ToolsClient("token", base_url=private_service.url)
    with pytest.raises(recurse.ToolsError, match="upload descriptor") as failure:
        client.prepare_private(
            write_tools_service(tmp_path / "project"), max_cost_micro_usd=200_000
        )
    assert failure.value.version_id == "version-1"
    assert len(private_service.requests) == 1
