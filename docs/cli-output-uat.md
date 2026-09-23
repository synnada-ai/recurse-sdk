# Run/status output UAT

From the SDK checkout, run `uv sync --locked`. This procedure uses the existing local HTTP test
service, disposable application files, and a file-backed test keychain. It does not use your saved
login or create hosted/billable runs.

## Inspect the command outcomes

Run the following and review the printed exit codes, YAML, and stderr:

```sh
uv run python - <<'PY'
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.conftest import write_app
from tests.keyring_backend import process_environment
from tests.test_cli import FakeService

service = FakeService()
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        app = write_app(root / "app")
        environment = process_environment(root, service.url)
        success = service.run_views[-1]
        failure = {
            **success,
            "status": "failed",
            "error": {"code": "execution_failed", "message": "Test execution failed."},
        }
        cases = [
            ("Successful run", ["run", str(app)], success),
            ("Failed run, successful observation", ["run", str(app)], failure),
            ("Inspect failed run", ["status", service.run_id], failure),
            ("Missing application argument", ["run"], success),
            ("Unknown run lookup", ["status", "missing-run"], success),
        ]
        service.fail_detail["/v1/runs/missing-run"] = (404, "Run not found.")
        for label, arguments, snapshot in cases:
            service.version_statuses = ["ready"]
            service.run_views = [snapshot]
            result = subprocess.run(
                [sys.executable, "-c", "from _recurse_cli import main; raise SystemExit(main())", *arguments],
                env=environment, capture_output=True, text=True, timeout=15,
            )
            print(f"\n{label}: exit {result.returncode}")
            print(result.stdout, end="")
            print(f"stderr: {result.stderr!r}")
finally:
    service.close()
PY
```

Expected results:

1. Successful run: exit `0`, `status: succeeded`, `cli_error: null`, empty stderr.
2. Failed run: exit `0`, `status: failed`, remote `error`, `cli_error: null`, empty stderr.
3. Inspect failed run: the same failed-run YAML and exit `0`.
4. Missing argument: exit `1`, `cli_error.code: invalid_arguments`, no invented run state,
   and an error diagnostic on stderr.
5. Unknown lookup: exit `1`, `cli_error.code: request_failed`, the requested run ID and recovery
   guidance, no invented run state, and an error diagnostic on stderr.

## Verify early output and interruption

Run the process-level checks:

```sh
uv run pytest -q tests/test_run_output.py::test_run_id_is_flushed_through_a_pipe_before_terminal_output tests/test_signals.py
```

The pipe check withholds the terminal response until the real CLI's run ID is readable on stdout;
a buffered or delayed ID fails the check. POSIX signal checks exercise Ctrl-C during admission,
polling, and cancellation, plus terminal suspension/resumption. Expect five passing tests on POSIX.
Ctrl-C exits `130` with valid YAML and `cli_error.code: interrupted`; only a validated cancellation
response supplies a remote `status`. Unconfirmed cancellation preserves recovery guidance.

Before integration, run `./check.sh` for formatting, lint, types, SDK coverage, package build, and
example checks. Release and website/plugin publication are separate steps tracked by SDK #58 and
Web #100; this UAT does not publish anything.
