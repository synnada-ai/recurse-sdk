# Recurse guide

## Installation

Python 3.14 or newer is required. Install Recurse from PyPI:

```sh
pip install recurse-sdk
```

The distribution provides the `recurse` package and command. Verify the installation with
`recurse --help`.

If you prefer uv, install the CLI as an isolated tool:

```sh
uv tool install recurse-sdk
```

Add the runtime package to an application that imports `recurse`:

```sh
uv add recurse-sdk
```

Running or deploying an application also requires [uv](https://docs.astral.sh/uv/getting-started/installation/)
on your PATH to build its package.

## Authoring an application

An application directory contains:

- `agent.yaml` — the manifest; see `reference/manifest.md` for every field.
- a system prompt file (declared by `agent.prompt`).
- a tool module (declared by `tools.source`).
- `pyproject.toml` with an explicit standards-based build backend, and the uv lockfile declared by
  `runtime.lockfile`.

### Tool interfaces

Every public module-level function in the tool module must be registered under
`tools.register`, fully type-annotated, and carry a Google-style docstring whose `Args:`
section describes each parameter. Tools that return a value must describe it under
`Returns:`; tools returning `None` must not have a `Returns:` section. Supply type parameters
for generic types, such as `list[Candidate]` rather than bare `list`, so Recurse can describe
and validate their contents. Bare collections are not unrestricted JSON containers: in strict
mode, bare `list` elements require stored-object references, and bare `dict` does not accept an
ordinary inline JSON object. Use `list[int]` for inline integers or `dict[str, str]` for inline
string values. For mixed values, declare the allowed types explicitly, for example
`dict[str, str | int | list[str] | None]`.

Recurse uses Python annotations to define tool parameters and results.
Supported examples include scalars (`bool`, `int`, `float`, `str`, `None`), your own classes,
`list[int]`, `dict[str, float]`, `tuple[int, ...]`, `tuple[int, str]`, optional values such as
`str | None`, unions such as `list[int] | str`, and `Literal["a", "b"]` (from `typing`).
Collections can also be nested, such as `list[dict[str, tuple[int, ...]]]`. Use a `TypedDict`,
dataclass, or Pydantic model with concrete field types for a structured inline object. If using
Pydantic, include it in your application's dependencies. Plain custom classes instead require
stored-object references; an inline dictionary is not a substitute for an instance.

The harness turns the docstring summary, body, and `Returns:` section into the tool description.
Each `Args:` entry describes a parameter, and type annotations supply the schema. Write these
as instructions the specialist can use: what the tool does, when to choose it, valid ranges or
units, side effects, and what another tool can do with the result. Keep signatures and parameter
instructions here; the agent prompt explains their role in the task.

Use safe defaults and make errors explain both the problem and what the specialist can correct.
Keep tools safe to retry where possible. Prefix private helpers with `_`.

### Passing values between tools

Tools can return Python objects that other tools accept directly. For example, put these two
functions and their shared type in `tools.py`:

```python
from dataclasses import dataclass
from math import hypot


@dataclass(frozen=True)
class Point:
    """A point in a two-dimensional coordinate system."""

    x: float
    y: float


def make_point(x: float, y: float) -> Point:
    """Create a point for measurement.

    Args:
        x: Horizontal coordinate.
        y: Vertical coordinate in the same units as x.

    Returns:
        The point, accepted by distance_from_origin.
    """
    return Point(x, y)


def distance_from_origin(point: Point) -> float:
    """Measure a point's distance from the origin.

    Args:
        point: Point to measure.

    Returns:
        Distance in the same units as the point's coordinates.
    """
    return hypot(point.x, point.y)
```

`make_point` returns a `Point` that `distance_from_origin` accepts without reconstructing it from
text. The harness resolves dependencies between calls: a call waits for the values it needs,
while independent calls can execute concurrently. Results can also be stored as Python objects
in program memory and reused across iterations.

Collections can contain application objects directly, such as `list[Candidate]`; ordinary
collections do not need wrapper classes. Stored class instances can be passed between tools by
reference. Repeated references retain object identity, so a mutation through one reference is
visible through the others. However, reference support inside an inline collection depends on
its element type: `list[PlainClass]` accepts individual object references, whereas `list[Point]`
for the dataclass above expects inline point objects. To pass existing points together, use a
reference to the whole stored list rather than individual references inside an inline list.

Pass shared state through typed parameters and return values. Mutable module globals hide
dependencies from the harness and can lead to incorrect execution schedules. Files under
`recurse.context().workspace` serve a different purpose: downloadable artifacts available after
the run.

#### Reading reference-related tool errors

These fields appear in the specialist's tool calls, not in your Python function signatures or
the inputs you send to a deployed MCP:

- `save_as: "point"` saves a storable tool result for later calls. `save_as: null` does not save it.
- `{"storage_key": "point"}` refers to the whole saved object.
- `{"storage_jsonpath": "points[0]"}` selects an item or field within a saved object.

`storage_key` is a literal name: `{"storage_key": "point.x"}` looks for an object named
`point.x`, not the `x` field of `point`. Use `{"storage_jsonpath": "point.x"}` for that field.
If an error says a stored-object reference was expected, check the receiving parameter's type:
use concrete types for inline data, or pass a reference to a compatible saved object. Changing
`no_storage` does not make an `Any` value accept inline data.

### Tool registration and settings

Set `tools.source` to the application-relative tool file and register every public tool function
by its exact name. A `~` entry uses defaults. For the example above:

```yaml
tools:
  source: tools.py
  register:
    make_point: ~
    distance_from_origin: ~
```

Use `tools.defaults` for shared settings and override individual registrations when needed:

- `storable` allows a return value to be saved in program memory; a non-`None` return type enables
  it by default. Disable it only for results used solely as context text or with no composable
  Python object. This storage is separate from workspace artifacts.
- `no_storage` removes the optional stored-object reference alternative for listed parameters.
  It does not relax type requirements: Recurse uses strict mode, so `Any`, including values
  inside `dict[str, Any]` or `list[Any]`, still requires stored-object references. Use concrete
  types for inline data, such as `dict[str, str]` for a dictionary of inline strings.
  Use it for identifiers or control flags where Python object passing is unnecessary.
  It defaults to an empty list.
- `volatile` defaults to `false`. Set it to `true` when identical arguments can produce different
  results; repeated calls or recurring patterns then do not alert the harness to a stuck loop.
- `pure_args` lists parameters the tool guarantees not to mutate in place. It defaults to an
  empty list; `null` declares all parameters pure. Recurse uses it to decide whether to refresh
  the stored-object previews shown to the specialist after a call. An incorrect declaration can
  leave those previews stale. It does not control execution order or make concurrent mutations
  safe. Keep the conservative default when unsure.

`tools.built_in` defaults to `true` and controls built-in tools such as notes, TODO management,
and planning. Set per-tool options only when their behavior calls for them.

Each application tool call has a timeout, separate from the account-funded run lifetime. It is set by
`agent.timeout_tools` in seconds (default 30); `0` or `null` disables it. A tool timeout is
reported to the specialist for recovery; it does not necessarily end the run immediately. Design
individual calls to fit that budget, breaking longer work into smaller steps where practical.

Two more optional `agent` settings bound repeated failures. `agent.max_llm_errors` (default 3) is
the number of consecutive model errors tolerated before the run stops. `agent.max_tool_errors`
(default 5) counts consecutive tool batches in which every call fails; any successful call resets
the count. `0` disables either limit, and omitting a setting keeps its default.

### Input and output contracts

Run inputs are declared as a JSON Schema (Draft 2020-12) under `inputs`. The resolved
`task` property is reserved: declare it as a string with either a non-empty default or a place
in `inputs.required`. After `{{ input.NAME }}` placeholders are resolved, it becomes the Agentia
user message and is excluded from `recurse.context().inputs`; the remaining values are available
to tools at run time.

The successful result is declared as a self-contained object JSON Schema under `outputs`. Finish
the agent with only a JSON object matching that schema. Files written to the run workspace remain
separate downloadable artifacts.

## Model configuration

Choose the specialist's model in `agent.yaml`:

```yaml
agent:
  prompt: prompt.md
  model: gpt-6-astra
```

Omit `model` to use the service default, currently `gpt-5.6-luna`. Use the usual `recurse run`
or `recurse deploy --as mcp` command; there is no model CLI flag. Both `run` and `status` report the
selected model in top-level `model` (`null` when unknown). If a later command error prevents
retrieving a snapshot, `run` retains any model learned during preparation. `deploy` prints it during
preparation. That selection stays with the prepared version even if the service default changes.
To change a deployed specialist's model, edit the declaration and deploy a new version.
Unsupported selections fail rather than falling back. Model usage is charged at the selected
model's rates, so the same token count can cost more with Astra.

## Prompt configuration

Set `agent.prompt` to the application-relative path of the system prompt file, such as `prompt.md`.
Use that file to describe the task's objective, constraints, and completion conditions. Function
signatures and parameter descriptions belong in tool docstrings.

Caller-specific instructions can use `{{ input.NAME }}` substitution in `inputs.task`. For example,
this fragment of `agent.yaml` declares a trial limit and includes it in the task:

```yaml
inputs:
  type: object
  required: [max_trials]
  properties:
    max_trials:
      type: integer
      minimum: 1
    task:
      type: string
      default: "Try at most {{ input.max_trials }} candidates and return the best measured result."
```

With `{"max_trials": 10}` as run inputs, the resolved task contains `Try at most 10 candidates`.
Tools can read the same value through `recurse.context().inputs["max_trials"]`.

For tool design, separating actions from independent validation is recommended so measurements
can guide the agent's next choice. A completion tool can check acceptance conditions, save the
result, and return measured facts. See the [agent design guidance](https://recurse.run/SKILL.md#agent-design-and-learning-from-evidence)
for the broader workflow. The [Tiny Tuner walkthrough](#tiny-tuner-walkthrough) shows a compact
SDK application; [Predictive Modeler](#predictive-modeler-walkthrough) covers multiple prediction tasks.

## Building application artifacts

```python
import recurse

artifacts, record = recurse.build_bundle("path/to/app")
```

The returned record has this complete shape:

```python
{
    "apiVersion": "recurse.application/v1alpha1",
    "source": {
        "sha256": "<lowercase SHA-256 of artifacts['source']>",
        "size_bytes": 123,
    },
}
```

`build_bundle` validates the manifest, declared files, and registered tools without importing your
tool code. It then runs `uv build --sdist`; your declared build backend selects the files shipped in
`artifacts["source"]`. Configure that backend to include `agent.yaml`, the prompt, the tool module,
and the exact `uv.lock`. Keep development files such as virtual environments, caches, and `.env`
out through the backend's normal inclusion rules. The sdist is identified by its SHA-256 and byte
size in `record`; rebuilding identical source is not guaranteed to reproduce identical sdist bytes.

For example, a Hatchling application can select its runtime files explicitly:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.sdist]
include = ["agent.yaml", "prompt.md", "tools.py", "uv.lock"]

[tool.hatch.build.targets.wheel]
include = ["agent.yaml", "prompt.md", "tools.py"]
```

The source distribution is limited to 64 MiB compressed and expanded and 1024 members. It must
have one standard root and contain only directories and regular files.

## The runtime context

During a run, tools read inputs and write outputs through the runtime context:

```python
import recurse


def my_tool(name: str) -> str:
    """Greet a name and record it.

    Args:
        name: Name to greet.

    Returns:
        The greeting.
    """
    greeting = f"hello {name}"
    (recurse.context().workspace / "greeting.txt").write_text(greeting)
    return greeting
```

`recurse.context()` returns the active `RunContext`. `context().inputs` is the deeply
read-only mapping of validated run inputs (without `task`); `context().workspace` is the
writable output directory. Every file written under the workspace is collected as a run
artifact (64 MB total limit). Calling `recurse.context()` outside an active run raises
`RunContextError`.

Use the workspace for artifact files and typed tool arguments for values shared between calls,
as shown in [Passing values between tools](#passing-values-between-tools).

## Login

```sh
recurse login
```

Your browser opens the Recurse website login (`recurse.run/login`); sign in with email and
password, Google or GitHub, the same account you use for gallery apps. If you are already signed
in there, confirm the account to continue. The CLI listens on `http://127.0.0.1:8765/callback`,
protects the flow with a PKCE challenge and a single-use state value, and stores one opaque
device credential in your
operating system keychain (service `recurse-cli`). Commands and MCP processes sharing that
login reuse a short-lived access token stored in the same keychain. A local lock coordinates
refreshes, so concurrent callers do not each exchange the device credential. Tokens are scoped
to both the active API URL and saved login, and refreshed thirty seconds before their returned
expiry. TEST and production logins can coexist. No credentials are written into application
directories, lock files, or MCP configuration.

Token reuse requires a writable keychain. If only saving a freshly issued token fails,
the command continues with that token and prints a warning to stderr. Later commands
must exchange again and may hit sign-in rate limits until keychain writes are restored.
Failures reading the keychain or removing a stale token still stop authentication.

Log out when you want to revoke the device credential:

```sh
recurse logout
```

The local login and cached token are removed only after revocation succeeds, so a temporary
service failure can be retried safely. Logging in again replaces the previous login's cache.
Remote revocation of a device credential prevents further refresh. Already issued access tokens
remain valid until expiry: general API sessions currently last fifteen minutes, while Tools
resource tokens last one hour. A running request may already hold its token. Local logout prevents
further cache reuse and clears both the general API and Tools caches.


## Runtime-secret management

User secrets are credentials you supply for your application, such as a GitHub token or simulator
API key. Recurse separately manages its own credentials for calling the model; your application
does not receive those credentials.

Store your token under a name, using two hidden prompts:

```sh
recurse secret set github-token
recurse secret list
```

For non-interactive use, pass the value only through standard input:

```sh
printf %s "$GITHUB_TOKEN" | recurse secret set github-token --from-stdin
```

`--from-stdin` preserves all bytes, including whitespace and a trailing newline. Values must be
non-empty UTF-8 and no larger than 32 KiB. There is deliberately no `--value` option. Repeating a
name creates and activates a new encrypted version; neither the API nor the CLI can reveal a
stored value.

Delete a secret interactively, or use `--yes` only in intentional automation:

```sh
recurse secret delete github-token
recurse secret delete github-token --yes
```

You can set the same name again after deletion. This creates a new secret with a new ID and version
1. Deletion still disables affected MCP deployments; recreate the deployment and reconnect it if
it needs the new secret.

Bind a logical secret name to an environment name on a direct run or permanent MCP deployment:

```sh
recurse run path/to/app --secret GITHUB_TOKEN=github-token
recurse deploy path/to/app --as mcp --secret GITHUB_TOKEN=github-token
```

In `--secret GITHUB_TOKEN=github-token`, `github-token` is the stored name and `GITHUB_TOKEN` is the
environment variable your application reads at run time. Neither name is the token value. Bound
user secrets are not injected during application preparation. Repeat `--secret` for more bindings.

Inside a tool, read the value and pass it directly to the service client that needs it:

```python
import os


def call_service() -> None:
    """Read the bound token inside a tool."""
    token = os.environ["GITHUB_TOKEN"]
    # Use token with your GitHub client here; do not print or return it.
```

Binding a user secret does not automatically send it to the model. However, tool output can become
model input: if a tool returns the token, that text could reach the model. Keep values out of the
manifest, command arguments, application archive, returned text, logs, artifacts, and other model
input. Credential isolation does not prevent your application code from disclosing a user secret.

Native run traces expose original tool arguments, results, and errors without redaction. If a
published application's tool emits a publisher-owned credential into those values, the caller who
owns the run can read it. See [Native run traces](#native-run-traces).

A run keeps the secret versions selected when it started, so rotation affects future runs without
changing one already in progress. Deleting a bound secret disables affected MCP deployments and
blocks future runs that need it. If a required value is deleted before use, the run ends with
`secret_unavailable` before the tool executes.

## Direct runs

```sh
recurse run path/to/app --inputs inputs.json --cpu 1 --memory-mib 1024 \
  --secret GITHUB_TOKEN=github-token
```

The CLI builds and prepares an immutable version, admits that version directly, and waits for
terminal state. It writes and flushes `run_id` to standard output as soon as admission is confirmed,
then appends the terminal snapshot to the same YAML document. Routine progress messages are
suppressed; standard error is reserved for error diagnostics. A later `recurse status <run-id>`
returns the same remote snapshot and CLI outcome, including the selected model. Direct runs do not
create an MCP deployment. Runs continue while the account can fund them, up to the fixed
24-hour provider limit with three minutes reserved for finalization. The CLI waits for the remote
outcome without imposing a separate run duration. Inputs must be one JSON object; omit
`--inputs` for `{}`, or use `--inputs -` to read standard input. Resource limits use the same ranges
and defaults as deployment.

Direct runs are preemptible by default. When Recurse detects an interruption, it may report
`preempted`; detection is best-effort, so not every interruption is guaranteed to receive that
status. The CLI does not automatically retry a run reported `preempted`. Tools may already have
performed external actions; inspect those effects before deciding whether to start a new run,
which may repeat them. To avoid Modal Function preemption, use
`recurse run path/to/app --non-preemptible`. This does not prevent other failures. The option costs
3× the otherwise-equivalent Function CPU and memory charge, not 3× the total run cost. Sandbox,
model-token, and preparation charges are unchanged. The option applies to direct runs, not MCP
deployments.

Ctrl-C during `recurse run` requests cancellation of the admitted run and exits `130`. The CLI
prints the state confirmed by the service: cancellation can race completion, and a request alone
does not prove the run has stopped. If confirmation fails or remains pending, execution and charges
may continue; use the printed commands to inspect or cancel the run:

```sh
recurse status <run-id>
recurse cancel <run-id>
recurse artifacts <run-id> --output results
```

If Ctrl-C interrupts the admission response, the CLI replays the same admission request with its
original idempotency key to recover the run ID before cancelling. It does not prepare another
version or use a new key. If the original request never arrived, this replay can admit the run
before cancelling it. If recovery also fails, the CLI warns that the run's identity and state are
unknown; do not blindly start another run. Recovery uses the existing finite HTTP retries and request
timeouts. A second Ctrl-C stops waiting for confirmation without claiming remote execution stopped.

Ctrl-C before admission stops local work cleanly; it does not promise that an already submitted
preparation was cancelled. Interrupting login closes its callback listener. Ctrl-Z retains native
terminal behavior: it suspends the local CLI, and `fg` or `bg` resumes it; remote work is not cancelled.
Ctrl-D still ends stdin input or aborts an unanswered prompt. Closing a terminal is not an explicit
run cancellation request.

Artifacts are available for 24 hours after completion. `recurse artifacts` downloads only when the
run snapshot reports `outputs.availability: available`; a finalized `outputs.artifacts: []` exits
successfully without printing a download. Pending inventories fail without claiming the run produced
no artifacts. After expiration, `recurse status` still shows retained artifact metadata, but
`recurse artifacts` refuses the expired download. Downloads refuse unsafe paths
and existing directories, verify size and SHA-256, and only then atomically replace the destination
file.

For automation, `recurse run` and `recurse status` write one YAML document to standard output,
including on handled command errors and Ctrl-C. The early run ID is available through a pipe;
read until command completion before treating the rest of the document as a complete outcome.
Each field is written once. The full application return value stays under `outputs.result`,
including an `answer` property. Ordinary Unicode remains readable UTF-8; terminal controls and
explicit bidirectional formatting controls are escaped. Parsing restores the original strings.
Help remains normal CLI help, and an uncatchable termination or broken output stream cannot
promise a completed document.

The two commands use the same exit-code meaning: whether the **CLI operation** completed.
YAML `status` describes the **remote execution**. One optional `error` field contains `type`,
`code`, and `message`: `type: engine` for a remote error or `type: cli` for a command error.
When neither fails, `error` is omitted. A command error takes precedence if a remote status is
also known; that confirmed state remains in `status`.

| Exit code | `recurse run` | `recurse status` |
| --- | --- | --- |
| `0` | Observed a terminal outcome, including a failed, cancelled, timed-out, or preempted run. | Retrieved a valid snapshot, including queued, running, or unsuccessful runs. |
| `1` | Command failed: invalid input, packaging/authentication/API/network failure, malformed response, observation timeout, or an internal CLI error. | Command failed: invalid arguments, authentication/API/network failure, malformed response, or an internal CLI error. |
| `130` | Interrupted by Ctrl-C; cancellation may or may not be confirmed. | Interrupted by Ctrl-C; this lookup does not cancel the run. |

**Breaking change from SDK 0.2.0:** a confirmed unsuccessful `run` now exits `0` instead of `1`;
handled CLI/API errors in these two commands exit `1` instead of `2`. Other commands retain their
existing exit codes. `recurse run ... && next-step` alone no longer gates on remote execution success.
Check the YAML status as well, using Python from an environment containing the SDK and PyYAML:

```sh
recurse run path/to/app --inputs inputs.json > run.yaml &&
python -c 'import sys, yaml; run = yaml.safe_load(open("run.yaml")); sys.exit(run.get("status") != "succeeded")' &&
echo "The remote run succeeded; continue here."
```

A confirmed failed execution has `error.type: engine` with its original code and message,
retains any available partial outputs, and exits `0`. For example, its outcome fields are:

```yaml
run_id: 77777777-7777-4777-8777-777777777777
status: failed
error:
  type: engine
  code: execution_failed
  message: The agent execution failed.
```

On a command failure, the document contains `error.type: cli`, `error.code`, and `error.message`
instead of an invented remote failure. Before admission, there may be no run ID. For example,
`recurse run` without an application argument exits `1` with an `invalid_arguments` CLI error. A
failed observation retains the run ID when available and structured recovery guidance:

```yaml
run_id: 77777777-7777-4777-8777-777777777777
recovery:
  message: Remote state is unconfirmed. Execution and charges may continue. Inspect this run before starting another run.
  inspect: recurse status 77777777-7777-4777-8777-777777777777
  cancel: recurse cancel 77777777-7777-4777-8777-777777777777
error:
  type: cli
  code: request_failed
  message: The Recurse service could not be reached.
```

For `error.type: cli`, `error.code` is `invalid_arguments`, `cli_error` for other expected local errors,
`authentication_failed`, `request_failed`, `interrupted`, or `internal_error`.
These are separate from the remote `error.code` values below. When admission identity is unknown,
do not blindly resubmit. A confirmed input rejection does not claim that remote execution may
continue.

Ctrl-C finishes the document with `error.type: cli` and `error.code: interrupted` and exits `130`.
After admission, it includes the cancellation response's `status` only when validated; this can be a
terminal state or a still-pending state. It does not invent result, cost, or artifact fields from
that limited response. If cancellation remains unconfirmed or pending, `recovery` gives the next
steps. Use `status` to retrieve the complete snapshot later.

For example, a completed run can produce:

```yaml
run_id: 77777777-7777-4777-8777-777777777777
schema_version: 1
model: gpt-6-astra
status: succeeded
created_at: '2026-09-22T12:00:00Z'
started_at: '2026-09-22T12:00:02Z'
completed_at: '2026-09-22T12:01:00Z'
resources:
  cpu_limit: 1.0
  memory_limit_mib: 1024
cost:
  currency: USD
  total_microusd: 12345
outputs:
  availability: available
  expires_at: '2026-09-23T12:01:00Z'
  result:
    answer: done
    score: 0.9
  artifacts: []
```

`outputs.availability` is `pending`, `available`, or `expired`. Artifact metadata can remain listed
after expiration even though `outputs.result` and downloads are no longer available.
`outputs.artifacts: []` means the finalized run produced no artifacts; `null` means the run is still
queued or running.

| Public reason | Meaning and next step |
| --- | --- |
| `insufficient_balance` | Check `recurse billing balance`. Redeem an available credit code or open checkout with `recurse billing top-up 5`, then retry. |
| `secret_unavailable` | A bound secret could not be supplied. Check `recurse secret list` and restore it with `recurse secret set NAME` if needed. |
| `invalid_inputs` | Check `--inputs` against the input schema in `agent.yaml`. |
| `invalid_agent` | Check the application declaration and packaged tool definitions. |
| `invalid_output` | Check the final return value against the declared output schema. |
| `execution_failed` | The agent failed. Follow the public explanation when available and keep the run ID when asking for help. |
| `artifact_failed` | Artifacts could not be collected or stored. Check their paths and retain the run ID. |
| `timed_out` | The service reports that the time limit was reached. Review the workload before starting another run. |
| `cancelled` | The service confirms cancellation. |
| `account_balance_exhausted` | The account ran out of available credit. Its active funded runs are stopped; add credit before choosing whether to run again. |
| `infrastructure_failed` | The public status is `failed`; retain the run ID when asking for help. |
| `preempted` | Reported interruption; the CLI does not automatically retry. Inspect external effects before deciding whether to start a new run. `--non-preemptible` avoids Modal Function preemption at 3× Function CPU and memory cost, but not other failures. |

Failure to observe a run is different from a failed run. `authentication_failed` directs you to
`recurse login`; `request_failed` means the CLI could not complete a service request, not that remote
execution stopped. These CLI failures exit `1` and use `error.type: cli`.

After an admitted run loses observation, the CLI retains its ID, warns that execution and charges
may continue, and includes `recurse status <run-id>` and `recurse cancel <run-id>` under `recovery`.
Inspect the existing run before starting another. If admission itself lost its response, the CLI
does not know whether a run was created and does not automatically resubmit it.
The Ctrl-C recovery described above is the explicit cancellation path.

### Native run traces

Retrieve the original model and tool events for an existing run through the REST API. This works
for direct runs and published-app runs, while execution is running and for 24 hours after completion.
Reading a trace does not start another billable run. `recurse status` remains a run snapshot; the
CLI has no trace command, and no SDK upgrade is required to call this endpoint.

Set `RUN_ID` to the existing run ID and `RECURSE_ACCESS_TOKEN` to a current account access token
for its owner. Use that token as a bearer credential, not a runtime secret or the device credential
stored by `recurse login`. The CLI does not provide a public token-export command.

API clients obtain `access_token` from `POST https://api.recurse.run/v1/auth/token`: exchange
a login code with `{"grant_type":"authorization_code","code":"...","code_verifier":"..."}`,
or renew an existing device credential with
`{"grant_type":"device_credential","device_credential":"..."}`. Send these as JSON request
bodies and keep credentials private. See the [login contract](https://github.com/synnada-ai/recurse-engine/blob/main/docs/reference/control-service.md#public-deployment-example)
for the browser login and PKCE exchange. App-scoped MCP OAuth tokens cannot access this account API.

```sh
curl --fail --silent --show-error \
  -H "Authorization: Bearer $RECURSE_ACCESS_TOKEN" \
  "https://api.recurse.run/v1/runs/$RUN_ID/trace?after=0&limit=20"
```

The JSON response contains `run_id`, `availability`, `expires_at`, `events`, and `next_after`.
Each record has `id`, `dispatch_id`, `sequence`, `producer`, `recorded_at`, and an `event` with
its native `type` and `payload`. For example, a successful tool event retains:

```json
{
  "type": "framework.agent.events.ToolSuccessEvent",
  "payload": {
    "event_type": "tool_success",
    "tool_name": "write_receipt",
    "tool_call_id": "call_123",
    "arguments": {"item": "tea", "count": 2},
    "result": "2 x tea"
  }
}
```

For a nonempty page, pass `next_after` as `after` in the next request. An empty page returns
`next_after: null`: keep the previous cursor. While the run is active, an empty page means you
are caught up; poll again with that cursor and inspect the run snapshot for completion.
`after` defaults to `0`; `limit` defaults to `20` and can be at most `100`. Pages stop around
1 MB of event payload, but a single larger event is returned whole. `id` orders records across
attempts, `dispatch_id` identifies the attempt, and `sequence` orders its records.

Trace `availability` is `pending` for queued or running execution, even if events exist;
`available` after completion with captured records; `unavailable` when no records were captured;
and `expired` after the 24-hour retention window. Expired trace rows are deleted. Cancelled or
interrupted runs retain only their committed prefix, which may be incomplete. Traces discarded
by earlier runtime releases cannot be recovered. Another account's run returns HTTP 404.

Events use the pinned producer's native serialization, including model request context, model
results, original tool arguments, results, errors, and cause chains. Preserve their payloads;
there is no Recurse field mapping or content redaction. Native named tuples serialize as arrays.
Unsupported values retain `payload: null` and `serialization_error`. Python traceback frames
and individual provider HTTP retries are not captured; this is not an HTTP wire dump. Runtime
environments and provider authorization headers are not dumped.

The run owner can read these values. For published apps, that owner is the caller; publishing
the app does not give its author access to callers' traces. Any application credential emitted
into a tool argument, result, or error is visible to the caller. Inspect sensitive content before
sharing a trace. The safe public snapshot error and declared application result remain separate.
See the [native trace API reference](https://github.com/synnada-ai/recurse-engine/blob/main/docs/reference/runtime-results.md#native-run-traces)
for the complete response example and serialization details.

## Deployment

```sh
recurse deploy path/to/app --as mcp --cpu 1 --memory-mib 1024 \
  --secret GITHUB_TOKEN=github-token
```

The CLI builds and uploads the application once, waits until it is ready, creates the deployment,
and prints the deployment identifier, endpoint, and resource defaults. `--cpu` accepts `0.125`
through `16` CPU in `0.125` increments;
`--memory-mib` accepts `512` through `16384` MiB in `128` MiB increments. Omitting them uses `1`
CPU and `1024` MiB. These are billable run ceilings, not consumption measurements. A failed build
reports `build_failed`; an account with too little balance reports the top-up and redemption
commands needed before retrying.

Local prompt, tool, dependency, or model edits do not update an existing deployment. Deploy again,
then replace the deployment ID in the MCP host configuration and reconnect to use the new version.

## Local MCP access

Run `recurse login` once. Use `recurse mcp serve` without a deployment identifier to
connect all active, published gallery apps through one server. Each tool is named
`apps.<gallery-slug>` and retains its own input schema. Runs and outputs belong to
the caller and consume that caller's wallet. Private apps are not included.

The bridge targets `/mcp/all`. Direct HTTP clients authorize that exact resource
through OAuth; consent includes current and future published apps. An existing
individual-app OAuth grant is not automatically widened.

To connect only one deployment, configure a local MCP host with its identifier:

```sh
codex mcp add recurse -- recurse mcp serve <deployment-id>
claude mcp add recurse -- recurse mcp serve <deployment-id>
```

For Codex, add the following timeouts to the `recurse` server section created in
`~/.codex/config.toml`. This host timeout allows 24 hours plus ten minutes for startup and
result delivery. Hosts can impose their own limits; configure enough time for your workload:

```toml
[mcp_servers.recurse]
command = "recurse"
args = ["mcp", "serve", "<deployment-id>"]
startup_timeout_sec = 180
tool_timeout_sec = 87000
```

Give other MCP hosts comparable startup and tool-call headroom around the execution limit.

Both commands start the same small stdio bridge. Each process reads the shared device
credential and reuses its short-lived access token through the operating system keychain.
The device credential is never written to host configuration, environment variables, or
standard output. Each tool call is submitted once and can continue while the local process polls
for its result. A host cancellation cancels that call; temporary connection interruptions resume
waiting for the same call instead of submitting it again.

Completed tool results, terminal task errors, and remote polling errors after task admission include
a `Task: task_…` reference for support.
This reference does not change the agent's declared structured output or artifact links.

An MCP host may override the deployment's resource ceilings for one call through standard request
metadata. Include either or both fields under `params._meta` in the `tools/call` request:

```json
{
  "recurse.run/resources": {
    "cpu_limit": 2,
    "memory_limit_mib": 2048
  }
}
```

Omitted resource fields retain the deployment defaults. The same CPU and memory ranges apply as
for `recurse deploy`; the agent declaration remains independent of hardware selection.

Completed tool results can contain `recurse://artifact/<run-id>/<output-id>` resource links. When
the host reads one, Recurse verifies its declared byte size and SHA-256 digest before returning the
contents. Artifact credentials and private storage locations are never exposed in the tool result.

## Billing

```sh
recurse billing balance
recurse billing top-up 5
recurse billing redeem CODE
```

`balance` shows both total balance and the amount not currently held for running work. `top-up`
accepts `$5.00` through `$500.00` with at most two decimal places and opens one-time hosted
Checkout; `$5`, `$10`, and `$20` are ordinary examples, not separate plans. On a headless machine,
add `--no-open` to print the hosted URL. `redeem` applies a Recurse-issued credit code once.

## Tiny Tuner walkthrough

`examples/tiny-tuner` is a complete application that tunes a tiny classifier over a
deterministic synthetic dataset:

1. `extract_features` expands the dataset with a chosen polynomial degree, optional
   interaction feature, and optional standardization. The classes are only separable
   when the interaction feature is present, so feature choice genuinely matters.
2. `train_model` fits a logistic classifier with seeded initialization and the
   momentum declared by the `optimizer_momentum` run input.
3. `validate_model` measures each candidate's F1 on the validation split and appends
   it to the accumulated history.
4. `save_best_model` writes the best validated model to `best-model.json` in the run
   workspace.

The agent runs measure-and-revise loops until a candidate reaches `target_f1` or
`max_trials` is exhausted, then saves the winner. Build it yourself:

```python
import recurse

artifacts, record = recurse.build_bundle("examples/tiny-tuner")
print(record["apiVersion"], record["source"]["sha256"], len(artifacts["source"]))
```

Run it directly with `recurse run examples/tiny-tuner --inputs inputs.json`, or deploy it with
`recurse deploy examples/tiny-tuner --as mcp`.

## Predictive Modeler walkthrough

`examples/predictive-modeler` searches scikit-learn pipelines for a dataset and prediction task.
Its shared input contract is `dataset`, natural-language `task`, optional structured `quality`,
and `budget`. Runnable real-data requests cover binary, multiclass, and multilabel classification,
regression, and single/panel forecasting.

1. `get_request` exposes structured inputs; `review_inputs` records the agent's first consistency
   check. Contradictory prose and quality stop with `inconsistent_inputs` before training.
2. `inspect_dataset` profiles the table. `resolve_problem` freezes targets, available features,
   task-specific metrics, and disjoint evaluation splits.
3. `train_candidate` fits bounded candidates in deadline-controlled subprocesses.
4. `evaluate_candidate` refits and scores fresh models on frozen cross-validation folds;
   `experiment_history` records every attempt.
5. `finish_run` chooses the best feasible measured candidate, tests that winner once, and writes
   a reloadable pipeline, report, contract, splits, and trial history. Failed final acceptance
   returns `no_feasible_model`, never a replacement chosen on test performance.

The agent chooses experiments from hypotheses and evidence. Tools enforce metrics, budget, and
artifact selection. Semantic interpretation is an agent responsibility. See the
[example README](../examples/predictive-modeler/README.md) for its support matrix and limitations.

Build the application locally:

```python
import recurse

artifacts, record = recurse.build_bundle("examples/predictive-modeler")
print(record["apiVersion"], record["source"]["sha256"], len(artifacts["source"]))
```

Run the binary request with:

```sh
recurse run examples/predictive-modeler \
  --inputs examples/predictive-modeler/inputs/binary.json --memory-mib 2048
```

Or deploy with `recurse deploy examples/predictive-modeler --as mcp --memory-mib 2048`.

## Tools-only packaging

A tools-only service has a separate [`tools.yaml` manifest](reference/tools-manifest.md) and runs
registered functions directly. It does not require `agent.yaml` or a prompt. Existing agent
applications keep their manifest and builder.

Declare a description, JSON input/output schemas, and explicit fixture pricing for each registered
function. Inputs are closed objects whose properties and required/default declarations match the
Python signature. Use named parameters, annotations, and Google-style docstrings. The current
authoring contract requires zero provider and compute bases. The service selects trusted billing
rates at publication, applying `1×` provider and `4×` compute pricing. Authors cannot supply provider
prices or measurement evidence through the source bundle.

Include `tools.yaml`, the declared source, and `uv.lock` in your standard build backend's source
distribution. Build it with the public Python SDK:

```python
import recurse

manifest = recurse.load_tools_manifest("path/to/app-tools")
artifacts, record = recurse.build_tools_bundle("path/to/app-tools")
print(manifest["metadata"]["name"], record["apiVersion"], len(artifacts["source"]))
```

The record's API version is `recurse.tools.application/v1alpha1`. Local validation parses the tool
source without importing it.

## Tools API

These APIs and commands require Recurse SDK 0.2.5 or newer. Defaults target the production
origins. For TEST, set `RECURSE_API_URL=https://api.dev.recurse.run` and
`RECURSE_TOOLS_URL=https://tools.dev.recurse.run` before login or invocation.

### Catalog and calls

Local Python explicitly reuses a saved `recurse login` credential:

```python
import uuid

import recurse

client = recurse.ToolsClient.from_login()
page = client.catalog_search("search", limit=20)
tool = client.catalog_get(page["tools"][0]["id"])
receipt = client.call(
    tool["id"],
    tool_version=tool["version"],
    price_version=tool["price_version"],
    inputs={"query": "example"},  # Match the inspected input schema.
    idempotency_key=str(uuid.uuid4()),
    max_cost_micro_usd=100_000,
)
receipt = client.get_call(receipt["call_id"])
# If still pending and no longer needed:
# receipt = client.cancel_call(receipt["call_id"])
```

Prices and charges are integer micro-USD (`1,000,000` is one US dollar). Inspect the service quote
before choosing a cap. The service rejects stale versions, invalid inputs, insufficient funds, or
an inadequate cap before admission. Preserve the same inputs, versions, cap, and idempotency key
when checking an ambiguous submission. Repeated `get_call` reads do not execute or charge again.
Accepted calls return durable receipts immediately; read again for results, errors, billing, and
retention status. A failed execution remains receipt data. Request failures raise `ToolsError`,
with `status_code` when an HTTP response exists.

The CLI exposes the same operations and prints JSON:

```sh
recurse tools search "search" --limit 20
recurse tools inspect <tool-id>
recurse tools call <tool-id> --tool-version <version> --price-version <price-version> \
  --inputs inputs.json --idempotency-key <stable-request-key> --max-cost-micro-usd 100000
recurse tools get <call-id>
recurse tools cancel <call-id>
```

Use `--inputs -` to read JSON from stdin. Accepted and retrieved receipts exit with code 0,
including failed execution receipts; argument and request failures exit with code 2. Neither the
CLI nor Python automatically retries an uncertain network submission or waits for execution.

### Private Tools runs

Stage 41b features below use the unreleased SDK 0.2.6 candidate on the development branch for
TEST validation. Build and install the current branch artifact; do not install a presumed public
0.2.6 release:

```sh
uv build --no-sources
uv tool install --force dist/recurse_sdk-0.2.6-py3-none-any.whl
```

Any enabled customer with sufficient wallet funds can prepare and run their own `tools.yaml`
project without publishing it. Use the same project format, locked dependencies and registered
function schemas described above. Private preparation and execution are billed at 4× compute
cost; the explicit cap includes both phases and any requested paid API calls. Omit `apis` for
compute-only Python. Approved read APIs through treg are available using the declaration below.

```sh
recurse tools run path/to/app-tools --function calculate --inputs inputs.json \
  --idempotency-key <stable-request-key> --max-cost-micro-usd 200000 \
  --timeout-seconds 600
recurse tools private-version <version-id>
recurse tools get <call-id>
recurse tools cancel <call-id>
```

`tools run` prepares the project, inspects the function's immutable price and versions, and
admits the call with the supplied cap and key. It prints the call ID to stderr before polling
and prints the final JSON receipt to stdout. `--timeout-seconds` limits each preparation and
execution wait; an execution wait timeout returns the latest pending receipt and leaves the
call running. Ctrl-C during execution requests cancellation, prints the cancellation receipt
when available, and exits with code 130. Cancellation and financial closure remain separate.
The SDK checks the selected function and its input schema locally before paid preparation.

Repeat the same project source, inputs, cap and idempotency key after an ambiguous submission.
Identical source reuses your stored version and preparation, and identical admission returns
the same call. The SDK checks for an existing admission before paid preparation, so an exact
retry still recovers the original call after its source image or runtime has been retired.
Changed source or arguments require a new key and are rejected before another preparation.
A preparation error retains `ToolsError.version_id`, which `private-version` can inspect.
Failed preparation can incur compute charges even though no execution was admitted. Inspect
that version before explicitly retrying; a later run may create and charge a new preparation.
The execution idempotency key protects admitted calls, not failed preparations before admission.
A polling error leaves the
printed call ID available for `get` or `cancel`. No network outcome is automatically retried.

Python's convenience method returns the durable admission receipt without waiting for execution:

```python
import recurse

client = recurse.ToolsClient.from_login()
receipt = client.run(
    "path/to/app-tools",
    "calculate",
    inputs={"values": [1, 2, 3]},
    idempotency_key="stable-private-request-key",
    max_cost_micro_usd=200_000,
)
receipt = client.get_call(receipt["call_id"])
```

For separate preparation and explicit inspection, use
`client.prepare_private(directory, max_cost_micro_usd=...)`,
`client.get_private_version(version_id)`, `client.inspect_private(version_id, function_name)`
and `client.call_private(...)`. The latter requires the inspected tool and price versions,
JSON inputs, the idempotency key and the same total workflow cap. Private versions and results
remain owner-scoped; public catalog publication remains restricted to the configured publisher.

### Approved read APIs in private tools

Inspect service-controlled API metadata before writing a private tool:

```sh
recurse tools apis search search --limit 20
recurse tools apis inspect exa.web.search
recurse tools apis inspect firecrawl.web.scrape
```

Python discovery uses `client.apis_search(query, limit=20, cursor=None)` and
`client.apis_get(endpoint_id)`. CLI search also accepts `--cursor` for a returned page cursor.
Search returns the service's `items` page; inspection includes its immutable version, input
schema and maximum request charge in micro-USD. Discovery does not submit a provider request.
No provider credential, customer-supplied rate or treg account is required.

Each function may declare `apis: [exa.web.search, firecrawl.web.scrape]` beside its schemas in
`tools.yaml`. The list is optional, contains at most 20 unique endpoint IDs and grants no API
access when omitted or empty. Only approved, available read endpoints can be frozen during
preparation. The customer cannot set a provider price, credential, arbitrary upstream URL or
HTTP method through this declaration.

Inside that function, `from recurse_apis import call` accesses the runtime-supplied helper.
`call(endpoint_id, arguments)` returns bounded native provider JSON. It can request only the
function's declared endpoints. The trusted parent validates the frozen contract and spending
authority, makes requests through treg and records actual charges. The isolated Python receives
no provider/treg credential and has no unrestricted network access. This helper is not a project
dependency and is unavailable during ordinary local execution.

Provider debits are charged at 1× and Recurse compute at 4×. The supplied total cap covers
preparation, execution and upstream liability; the service guards spending before submitting
each request. Completed paid requests remain chargeable if later Python fails. An uncertain
provider outcome stays reconcilable instead of silently triggering another chargeable request.
There are at most 20 requests per execution, within existing 64 KiB input, 256 KiB response/result
and five-minute execution limits.

The [company-updates example](../examples/company-updates/README.md) performs up to six dependent
reads: it searches three official company domains, fetches one returned page per company, and
returns short excerpts with explicit missing-data reasons and provider dates. Its $0.30 example
authorization covers both preparation/execution reserves and those bounded reads; inspect current
metadata before running it. It contains no prompt or internal agent loop.

### Server authentication

Server Python takes an explicit short-lived bearer scoped to the exact Tools resource
`https://tools.recurse.run/mcp` (or the configured TEST resource):

```python
import os

import recurse

client = recurse.ToolsClient(
    access_token=os.environ["TOOLS_ACCESS_TOKEN"],
    base_url=os.environ["RECURSE_TOOLS_URL"],
)
print(client.catalog_search())
```

Obtain this bearer through the configured issuer's existing OAuth grant. The registered
`recurse-cli` refresh grant accepts `resource=<Tools origin>/mcp` with its durable CLI credential;
the existing MCP OAuth account-linking flow also issues a resource bearer. A general API session,
agent MCP bearer, provider key, or account ID is not a Tools credential. Server clients use no
browser or keychain and do not refresh automatically; replace an expired bearer explicitly.
M4 adds no long-lived API-key management or public CLI token-export command.

`ToolsClient.from_login()` obtains this resource bearer through the shared keychain and API-origin
refresh lock, without opening a browser. Its cache is separate from agent/API tokens and tied to
the saved login and exact Tools resource. A rejected bearer refreshes once with the original
request body and idempotency key. Network failures are not retried. Logout revokes the durable
credential and clears local caches; already issued resource bearers expire after one hour.

### Publication

Only the service's configured `agent@recurse.run` publisher account may stage and publish tools.
Other accounts can discover and consume published tools and run their own projects privately.
The publisher uploads a standard
`tools.yaml` bundle, waits for that version's preparation, and activates its catalog entries:

```sh
recurse tools publish path/to/app-tools --config publication.json --timeout-seconds 600
recurse tools version <version-id>
recurse tools publish-version <version-id> --config publication.json
```

Python uses `client.publish(directory, publication_config=...)`, `client.get_version(version_id)`,
and `client.publish_version(version_id, publication_config)`. Configuration defaults to
`{"tools": {}}`, which publishes validated functions with free fixture billing. Paid compute uses
the existing `provider-none-v1` selection, for example:

```json
{"tools": {"compute": {"billing": {"provider_rate_version": "provider-none-v1"}}}}
```

Supported external providers require service-approved rate versions and publisher-owned logical
secret bindings. Keep credential values out of the manifest and publication JSON. Provider
coverage beyond M3's supported adapter belongs to Stage 42.

An upload, preparation, or activation failure after staging raises `ToolsError` with `version_id`.
Inspect that version before retrying publication; the SDK does not stage another version
automatically. A timeout stops local waiting and leaves the remote version available for inspection.

### Tools MCP bridge

Configure an MCP host to launch `recurse tools mcp serve` after local login:

```sh
codex mcp add recurse-tools -- recurse tools mcp serve
claude mcp add recurse-tools -- recurse tools mcp serve
```

The Stage 41b service advertises thirteen generic operations through both this bridge and its
remote MCP endpoint. The bridge forwards the server's tools and arguments without maintaining
a provider list:

| Operations | Purpose |
| --- | --- |
| `catalog_search`, `catalog_get` | Discover and inspect published tools. |
| `call`, `get_call`, `cancel_call` | Admit, read and cancel durable executions. |
| `apis_search`, `apis_get` | Discover approved read API metadata; search accepts `query`, `limit` and an optional `cursor`. |
| `private_stage`, `private_complete` | Stage a standard source bundle and complete its scoped HTTP upload. |
| `private_get_version`, `private_inspect` | Read owner-scoped preparation and immutable function contracts. |
| `private_replay`, `private_call` | Recover an exact admitted project request or admit an inspected private function. |

Adding an approved API to the server catalog changes discovery results without changing these
operations or the installed SDK/plugin. MCP results are ordinary responses, with no Tasks.
A pending call returns its receipt; the host reads `get_call` until completion. stdout contains
only MCP frames. Existing `recurse mcp serve` continues to serve agent apps through its original bridge.

### Private authoring through remote MCP

Connect a remote MCP client to the matching TEST endpoint `https://tools.dev.recurse.run/mcp`
using its normal OAuth connection. Remote authoring requires a client that can build or obtain
the standard source bundle and perform the scoped HTTP upload. A client with only MCP transport
and no bundle/HTTP capability cannot perform the source-transfer step automatically. Local
`recurse tools run` already performs these steps through the SDK.

1. Create the normal `tools.yaml` project and dependency lockfile. Use
   `recurse.build_tools_bundle(directory)` to obtain `artifacts["source"]` and its
   `application_record`, or supply the same verified standard source distribution from another
   builder. The existing source limit is 64 MiB compressed and expanded, with at most 1024 members.
   Source bytes do not enter the 64 KiB MCP request body.
2. Before new paid preparation, call `private_replay` with `service_name`, the actual
   `source_sha256`, `function_name`, `inputs`, the stable `idempotency_key` and the original
   total `max_cost_micro_usd`. An existing exact request returns its original receipt. Changed
   source, inputs or authority conflict before another preparation. A null receipt means no
   execution has been admitted for that key.
3. Call `private_stage` with `service_name`, the bundle's `application_record` and the total
   `max_cost_micro_usd`. It returns `version_id`, `status` and, when an upload is needed, a
   `source_upload` descriptor. It accepts source metadata, not inline Python or a path on the
   remote server. A reused prepared or pending version may not need another upload.
4. Use the descriptor's exact `upload_url` for one HTTP POST. Set `Authorization` to its
   `upload_authorization` and `Content-Type` to `upload_content_type`. The body is the decoded
   base64 `upload_prefix`, followed by the unchanged source bytes, followed by the decoded
   `upload_suffix`. Use the descriptor as an opaque, scoped capability; do not replace its
   authorization with the Recurse bearer or forward it to another destination.
5. Call `private_complete` with `version_id` and the descriptor's `upload_id` as
   `source_upload_id`. If the transfer acknowledgment was lost, check this same completion
   identity before resending source or staging another version. Read `private_get_version`
   with `version_id` until preparation is terminal. Failed preparation may incur compute
   charges and must be inspected before an explicit retry.
6. Once prepared, call `private_inspect` with `version_id` and `function_name`. Pass the returned
   immutable `version` as `tool_version` and `price_version` into `private_call`, together with
   `version_id`, `function_name`, `inputs`, the stable `idempotency_key` and the original total
   `max_cost_micro_usd`. Poll `get_call` for results or request `cancel_call` with its `call_id`.

SDK callers who need these separate phases use
`client.stage_private(service_name, application_record, max_cost_micro_usd=...)` and
`client.complete_private(version_id, source_upload_id)`. `prepare_private` and `run` reuse that
same stage/upload/complete contract; no additional archive format or execution path is introduced.

`recurse login` uses the website login and standard OAuth code, refresh and revocation endpoints.
Existing saved login credentials continue to work after upgrading.
