"""Run command output separates local command failure from remote execution state."""

import selectors
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

import _recurse_cli as cli
from tests import test_cli
from tests.conftest import write_app
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService

_ephemeral_callback_port = test_cli._ephemeral_callback_port
keychain = test_cli.keychain
logged_in = test_cli.logged_in
service = test_cli.service


def document(text: str) -> dict[str, Any]:
    """Require one mapping without duplicate top-level fields before decoding it."""
    nodes = list(yaml.compose_all(text))
    assert len(nodes) == 1
    node = nodes[0]
    assert isinstance(node, yaml.MappingNode)
    keys = [key.value for key, _value in node.value]
    assert len(keys) == len(set(keys))
    result = yaml.safe_load(text)
    assert isinstance(result, dict)
    return result


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "timed_out", "preempted"])
def test_remote_outcome_is_only_in_yaml(  # noqa: PLR0913,PLR0917 - fixtures plus outcome
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
) -> None:
    """A remote failure must not turn successful run observation into a CLI failure."""
    del logged_in
    view = service.run_views[-1]
    view["status"] = state
    view["error"] = None if state == "succeeded" else {"code": state, "message": "Remote failure."}
    service.run_views = [view]
    monkeypatch.setattr(cli, "_POLL_SECONDS", 0)
    assert cli.main(["run", str(write_app(tmp_path / "app"))]) == 0
    captured = capsys.readouterr()
    assert document(captured.out) == {**view, "cli_error": None}
    assert captured.err == ""
    assert cli.main(["status", service.run_id]) == 0
    status = capsys.readouterr()
    assert status.out == captured.out
    assert status.err == ""


@pytest.mark.parametrize(
    "argv", [["run"], ["status"], ["run", "app", "--cpu", "bad"], ["status", "id", "--unknown"]]
)
def test_argument_errors_are_yaml_command_failures(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Parser failures must obey the run/status contract before any remote work."""
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    view = document(captured.out)
    assert view["cli_error"]["code"] == "invalid_arguments"
    assert "status" not in view
    assert "error:" in captured.err


@pytest.mark.parametrize("stage", ["inputs", "preparation", "admission", "observation", "status"])
def test_command_failures_preserve_identity_without_inventing_remote_failure(  # noqa: PLR0913,PLR0917 - fixtures plus failure stage
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stage: str,
) -> None:
    """Failed command output must remain parseable, retain recovery, and avoid a fake run state."""
    del logged_in
    monkeypatch.setattr(cli, "_POLL_SECONDS", 0)
    argv = ["run", str(write_app(tmp_path / "app"))]
    if stage == "inputs":
        argv += ["--inputs", str(tmp_path / "missing.json")]
    elif stage == "preparation":
        service.version_statuses = ["failed"]
    elif stage == "admission":
        service.fail_detail["/v1/runs"] = (500, "private server trace")
    else:
        service.fail_detail[f"/v1/runs/{service.run_id}"] = (500, "private server trace")
        if stage == "status":
            argv = ["status", service.run_id]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    view = document(captured.out)
    assert view["cli_error"]["message"]
    assert "status" not in view
    assert "error" not in view
    assert "private" not in captured.out + captured.err
    if stage in {"observation", "status"}:
        assert view["run_id"] == service.run_id
        assert view["recovery"]["inspect"] == f"recurse status {service.run_id}"
    elif stage == "admission":
        assert view["admission_reference"].startswith("run_")
        assert "may continue" in view["recovery"]["message"]
    else:
        assert "run_id" not in view
        assert "recovery" not in view


@pytest.mark.parametrize("command", ["run", "status"])
def test_unexpected_cli_error_is_structured_and_sanitized(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    """Unexpected implementation errors must not expose secrets or pretend the run failed."""

    def fail(*args: object, **kwargs: object) -> Any:
        """Simulate a failing local authentication dependency."""
        raise RuntimeError("private credential")

    monkeypatch.setattr(cli, "_access_token", fail)
    monkeypatch.setattr(cli, "_build_bundle", fail)
    assert cli.main([command, "app-or-id"]) == 1
    captured = capsys.readouterr()
    view = document(captured.out)
    assert view["cli_error"]["code"] == "internal_error"
    assert "private" not in captured.out + captured.err
    assert "status" not in view


def test_run_id_is_flushed_through_a_pipe_before_terminal_output(
    service: FakeService, tmp_path: Path
) -> None:
    """Buffering the run ID until completion would deadlock this real CLI consumer."""
    service.version_statuses = ["ready"]
    service.run_views = [service.run_views[-1]]
    waiting = threading.Event()
    release = threading.Event()
    original = service.route

    def route(
        method: str, path: str, body: dict[str, Any] | bytes | None, token: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Hold the terminal response until the consumer receives the admitted ID."""
        if method == "GET" and path == f"/v1/runs/{service.run_id}":
            waiting.set()
            assert release.wait(15)
        return original(method, path, body, token)

    service.route = route  # type: ignore[method-assign]  # external HTTP boundary
    app = write_app(tmp_path / "app")
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter and test-owned arguments
        [
            sys.executable,
            "-c",
            "from _recurse_cli import main; raise SystemExit(main())",
            "run",
            str(app),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=process_environment(tmp_path, service.url),
    )
    try:
        assert waiting.wait(10)
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=2), "run ID was not flushed before terminal state"
        early = process.stdout.readline()
        assert yaml.safe_load(early) == {"run_id": service.run_id}
        assert process.poll() is None
        release.set()
        remaining, stderr = process.communicate(timeout=10)
        assert process.returncode == 0
        view = document((early + remaining).decode())
        assert view["status"] == "succeeded"
        assert view["cli_error"] is None
        assert stderr == b""
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_status_interruption_is_yaml_and_does_not_cancel(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Interrupting a lookup must retain its identity without cancelling remote work."""
    del logged_in

    def interrupt(*args: object, **kwargs: object) -> Any:
        """Interrupt only the local network observation."""
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_get_run", interrupt)
    assert cli.main(["status", service.run_id]) == 130
    captured = capsys.readouterr()
    view = document(captured.out)
    assert view["run_id"] == service.run_id
    assert view["cli_error"]["code"] == "interrupted"
    assert "status" not in view
    assert view["recovery"]["inspect"] == f"recurse status {service.run_id}"
    assert not any(path.endswith("/cancel") for _, path, _, _ in service.requests)


def test_internal_error_after_admission_preserves_the_already_printed_id(
    service: FakeService,
    logged_in: dict[tuple[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Unexpected polling errors must finish the same document and retain recovery."""
    del logged_in
    monkeypatch.setattr(cli, "_prepare", lambda *_args, **_kwargs: ("access-1", "version-1"))
    original = cli._run_request

    def request(method: str, path: str, **kwargs: Any) -> tuple[dict[str, Any], str]:
        """Admit through real HTTP, then fail before observing remote state."""
        if method == "GET":
            raise RuntimeError("private internal detail")
        return original(method, path, **kwargs)

    monkeypatch.setattr(cli, "_run_request", request)
    assert cli.main(["run", "app"]) == 1
    captured = capsys.readouterr()
    view = document(captured.out)
    assert view["run_id"] == service.run_id
    assert view["admission_reference"].startswith("run_")
    assert view["cli_error"]["code"] == "internal_error"
    assert view["recovery"]["cancel"] == f"recurse cancel {service.run_id}"
    assert "status" not in view
    assert "private" not in captured.out + captured.err
