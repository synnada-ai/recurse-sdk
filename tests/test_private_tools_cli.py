"""Private Tools CLI runs retain durable receipts across waiting and cancellation."""

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import _recurse_cli as cli
from tests.test_cli import FakeService
from tests.test_tools_bundle import write_tools_service
from tests.test_tools_cli import keychain, logged_in, service, tools_service

__all__ = ["keychain", "logged_in", "service", "tools_service"]


@pytest.fixture
def private_service(tools_service: FakeService, monkeypatch: pytest.MonkeyPatch) -> FakeService:
    """Serve the owned project flow through the actual login and HTTP stack."""
    original = tools_service.route

    def route(  # noqa: PLR0911 - explicit private HTTP lifecycle responses
        method: str, path: str, body: Any, token: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Prepare one source and finish the admitted call on its first status read."""
        if path == "/v1/private-calls/replay":
            return 200, {"receipt": None}
        if path == "/v1/private-tool-versions":
            return 201, {
                "version_id": "version-1",
                "status": "staged",
                "source_upload": tools_service.registration["source_upload"],
            }
        if path == "/direct-upload":
            return 201, {}
        if path == "/v1/private-tool-versions/version-1/complete":
            assert body == {"source_upload_id": "source-upload-9"}
            return 200, {"version_id": "version-1", "status": "pending"}
        if path == "/v1/private-tool-versions/version-1":
            return 200, {"version_id": "version-1", "status": "prepared"}
        if path == "/v1/private-tool-versions/version-1/tools/echo":
            return 200, {"version": "tool-1", "price_version": "price-1"}
        if path == "/v1/private-calls":
            assert body == {
                "version_id": "version-1",
                "function_name": "echo",
                "tool_version": "tool-1",
                "price_version": "price-1",
                "inputs": {"message": "hello"},
                "idempotency_key": "private-cli-key",
                "max_cost_micro_usd": 200000,
            }
            return 200, {"call_id": "call-1", "status": "pending", "billing_status": "reserved"}
        if path == "/v1/calls/call-1":
            return 200, {
                "call_id": "call-1",
                "status": "succeeded",
                "billing_status": "closed",
                "charged_micro_usd": 23,
                "result": {"message": "hello"},
            }
        if path == "/v1/calls/call-1/cancel":
            return 200, {"call_id": "call-1", "status": "cancelled", "billing_status": "reserved"}
        return original(method, path, body, token)

    monkeypatch.setattr(tools_service, "route", route)
    return tools_service


def run_command(directory: Path) -> list[str]:
    """Build an explicitly approved private run command using stdin JSON."""
    return [
        "tools",
        "run",
        str(directory),
        "--function",
        "echo",
        "--inputs",
        "-",
        "--idempotency-key",
        "private-cli-key",
        "--max-cost-micro-usd",
        "200000",
        "--timeout-seconds",
        "60",
    ]


def test_private_cli_prepares_and_polls_existing_receipts(
    private_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A private run prints the final result and charges without publishing or resubmitting."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"message":"hello"}'))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    assert cli.main(run_command(write_tools_service(tmp_path / "project"))) == 0
    output = capsys.readouterr()
    receipt = json.loads(output.out)
    assert receipt["result"] == {"message": "hello"}
    assert receipt["charged_micro_usd"] == 23
    assert output.err == 'Call ID: "call-1"\n'
    paths = [path for _method, path, _body, _token in private_service.requests]
    assert paths.count("/v1/private-calls") == 1
    assert paths.count("/v1/calls/call-1") == 1
    assert not any("/publication" in path for path in paths)


def test_private_cli_can_inspect_preparation_after_lost_wait(
    private_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An owned preparation version can be read independently without source upload."""
    del logged_in
    assert cli.main(["tools", "private-version", "version-1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"version_id": "version-1", "status": "prepared"}
    assert private_service.requests[-1][1] == "/v1/private-tool-versions/version-1"


def test_private_cli_wait_timeout_returns_pending_receipt(
    private_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A local wait timeout leaves the admitted call inspectable and does not cancel it."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"message":"hello"}'))
    clocks = iter([0.0, 0.0, 61.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clocks))
    assert cli.main(run_command(write_tools_service(tmp_path / "project"))) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {"call_id": "call-1", "status": "pending", "billing_status": "reserved"}
    assert not any(path.endswith("/cancel") for _, path, _, _ in private_service.requests)


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_private_cli_interrupt_requests_cancellation(  # noqa: PLR0913,PLR0917 - HTTP fixtures and cancellation case
    private_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cancel_fails: bool,
) -> None:
    """Ctrl-C requests cancellation once and retains the call ID even if acknowledgment fails."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"message":"hello"}'))

    def interrupt(_seconds: float) -> None:
        """Represent the user interrupting an admitted run's wait."""
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)
    if cancel_fails:
        private_service.fail_detail["/v1/calls/call-1/cancel"] = (503, "unavailable")
    assert cli.main(run_command(write_tools_service(tmp_path / "project"))) == 130
    output = capsys.readouterr()
    assert 'Call ID: "call-1"' in output.err
    assert "Interrupted." in output.err
    if cancel_fails:
        assert output.out == ""
        assert "Cancellation was not confirmed" in output.err
    else:
        assert json.loads(output.out)["status"] == "cancelled"
    assert len([item for item in private_service.requests if item[1].endswith("/cancel")]) == 1


@pytest.mark.parametrize("call_id", [None, "", 1])
def test_private_cli_rejects_missing_admission_identity(  # noqa: PLR0913,PLR0917 - HTTP fixtures and malformed identity
    private_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    call_id: object,
) -> None:
    """A malformed admission cannot become an untraceable polling request."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"message":"hello"}'))
    private_service.malformed["/v1/private-calls"] = {"call_id": call_id}
    assert cli.main(run_command(write_tools_service(tmp_path / "project"))) == 2
    assert "missing call_id" in capsys.readouterr().err


def test_private_cli_polling_error_retains_call_identity(
    private_service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed receipt read never repeats admission and leaves an explicit resume identity."""
    del logged_in
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"message":"hello"}'))
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    private_service.fail_detail["/v1/calls/call-1"] = (503, "unavailable")
    assert cli.main(run_command(write_tools_service(tmp_path / "project"))) == 2
    assert 'Call ID: "call-1"' in capsys.readouterr().err
    assert len([item for item in private_service.requests if item[1] == "/v1/private-calls"]) == 1
