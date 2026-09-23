"""Process-level interruption checks against the local Recurse service double."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import write_app
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService


@pytest.fixture
def service() -> Iterator[FakeService]:
    """Keep all subprocess network traffic in one disposable loopback service."""
    fake = FakeService()
    fake.version_statuses = ["ready"]
    yield fake
    fake.close()


def start_cli(service: FakeService, app: Path) -> subprocess.Popen[bytes]:
    """Run the real CLI with a fake keychain and its actual packaging/HTTP paths."""
    environment = process_environment(app.parent, service.url)
    command = [
        sys.executable,
        "-u",
        "-c",
        "import sys; from _recurse_cli import main; raise SystemExit(main(sys.argv[1:]))",
        "run",
        str(app),
        "--inputs",
        "-",
    ]
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and test-owned arguments
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        process_group=0,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group signals")
@pytest.mark.parametrize("stage", ["admission", "polling", "cancellation"])
def test_sigint_cancels_or_reports_uncertainty(
    service: FakeService, tmp_path: Path, stage: str
) -> None:
    """Real SIGINT preserves admission identity and can interrupt cancellation waiting."""
    reached = threading.Event()
    cancelling = threading.Event()
    release = threading.Event()
    original = service.route
    service.run_views = [{**service.run_views[0], "status": "running"}]

    def route(
        method: str, path: str, body: dict[str, Any] | bytes | None, token: str | None
    ) -> tuple[int, dict[str, Any]]:
        """Synchronize signals with actual requests instead of timing sleeps."""
        response = original(method, path, body, token)
        if stage == "admission" and path == "/v1/runs" and not reached.is_set():
            reached.set()
            assert release.wait(15)
        elif stage != "admission" and method == "GET" and path.endswith(service.run_id):
            reached.set()
        elif stage == "cancellation" and path.endswith("/cancel"):
            cancelling.set()
            assert release.wait(15)
        return response

    service.route = route  # type: ignore[method-assign]  # script the external service boundary
    process = start_cli(service, write_app(tmp_path / "app"))
    try:
        assert process.stdin is not None
        process.stdin.write(b"{}")
        process.stdin.close()  # EOF supplies inputs; it must not cancel the run.
        process.stdin = None
        assert reached.wait(10), "CLI never reached the selected request boundary"
        process.send_signal(signal.SIGINT)
        if stage == "cancellation":
            assert cancelling.wait(5)
            process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130
        assert b"Traceback" not in stderr
        assert sum(path.endswith("/cancel") for _, path, _, _ in service.requests) == 1
        admissions = [body for _, path, body, _ in service.requests if path == "/v1/runs"]
        assert len(admissions) == (2 if stage == "admission" else 1)
        assert all(body == admissions[0] for body in admissions)
        view = yaml.safe_load(stdout)
        assert view["run_id"] == service.run_id
        assert view["cli_error"]["code"] == "interrupted"
        if stage == "cancellation":
            assert "may continue" in view["recovery"]["message"]
            assert "status" not in view
        else:
            assert view["status"] == "cancelled"
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.skipif(os.name != "posix", reason="POSIX terminal suspension")
def test_sigtstp_and_sigcont_keep_native_job_control(service: FakeService, tmp_path: Path) -> None:
    """Ctrl-Z suspends locally; resume and stdin EOF allow the run to finish normally."""
    process = start_cli(service, write_app(tmp_path / "app"))
    try:
        # The CLI is waiting for stdin EOF: no admission or cancellation is permitted yet.
        assert process.stdin is not None
        process.stdin.write(b"{}")
        process.stdin.flush()
        process.send_signal(signal.SIGTSTP)
        deadline = time.monotonic() + 5
        stopped = False
        while time.monotonic() < deadline:
            pid, status = os.waitpid(process.pid, os.WUNTRACED | os.WNOHANG)
            if pid:
                stopped = os.WIFSTOPPED(status)
                break
            time.sleep(0.01)
        assert stopped, "SIGTSTP did not suspend the CLI"
        assert service.requests == []
        process.send_signal(signal.SIGCONT)
        stdout, stderr = process.communicate(timeout=10)  # EOF resumes normal input consumption.
        assert process.returncode == 0
        assert b"status: succeeded" in stdout
        assert stderr == b""
        assert yaml.safe_load(stdout)["cli_error"] is None
        assert not any(path.endswith("/cancel") for _, path, _, _ in service.requests)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
