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

Each application tool call has a timeout, separate from the 15-minute run limit. It is set by
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

Your browser opens the hosted Recurse login page; sign in with GitHub or Google. The
CLI listens on `http://127.0.0.1:8765/callback`, protects the flow with a PKCE
challenge and a single-use state value, and stores one opaque device credential in your
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
Remote revocation of a device credential does not immediately invalidate an already-issued
access token: local commands can reuse it until refresh is due (currently within fifteen
minutes). A running request may already hold that token. Local logout prevents further cache reuse.

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
create an MCP deployment and have a 15-minute execution limit. Inputs must be one JSON object; omit
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
`authentication_failed`, `request_failed`, `observation_timeout`, `interrupted`, or `internal_error`.
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
| `infrastructure_failed` | The public status is `failed`; retain the run ID when asking for help. |
| `preempted` | Reported interruption; the CLI does not automatically retry. Inspect external effects before deciding whether to start a new run. `--non-preemptible` avoids Modal Function preemption at 3× Function CPU and memory cost, but not other failures. |

Failure to observe a run is different from a failed run. `authentication_failed` directs you to
`recurse login`; `request_failed` means the CLI could not complete a service request, not that remote
execution stopped. `observation_timeout` means local polling ended without confirmation, not that
the service reported `timed_out`. These CLI failures exit `1` and use `error.type: cli`.

After an admitted run loses observation, the CLI retains its ID, warns that execution and charges
may continue, and includes `recurse status <run-id>` and `recurse cancel <run-id>` under `recovery`.
Inspect the existing run before starting another. If admission itself lost its response, the CLI
does not know whether a run was created and does not automatically resubmit it.
The Ctrl-C recovery described above is the explicit cancellation path.

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

Run `recurse login` once, then configure each local MCP host with the deployment identifier:

```sh
codex mcp add recurse -- recurse mcp serve <deployment-id>
claude mcp add recurse -- recurse mcp serve <deployment-id>
```

For Codex, add the following timeouts to the `recurse` server section created in
`~/.codex/config.toml`. The 19-minute host timeout outlives bridge startup, the 15-minute execution
limit, and worst-case request/result delivery:

```toml
[mcp_servers.recurse]
command = "recurse"
args = ["mcp", "serve", "<deployment-id>"]
startup_timeout_sec = 180
tool_timeout_sec = 1140
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
