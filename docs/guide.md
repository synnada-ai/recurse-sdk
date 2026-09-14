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
for generic types whenever possible, such as `list[Candidate]` rather than bare `list`, so the
harness can reflect them in the tool schema. Use bare generics only when their element types
are unconstrained or unknown.

The harness turns the docstring summary, body, and `Returns:` section into the tool description.
Each `Args:` entry describes a parameter, and type annotations supply the schema. Write these
as instructions the specialist can use: what the tool does, when to choose it, valid ranges or
units, side effects, and what another tool can do with the result. Keep signatures and parameter
instructions here; the agent prompt explains their role in the task.

Use safe defaults and make errors explain both the problem and what the specialist can correct.
Keep tools safe to retry where possible. Prefix private helpers with `_`.

### Passing values between tools

Return Python objects that other tools can accept directly. For example, the
[skill's polygon example](https://recurse.run/SKILL.md#authoring-tools) has `build_polygon` return a
`Polygon` and `measure_area` accept that object. The specialist can connect the tools without
reconstructing the polygon from prose. Explain useful combinations in the return description.

The harness resolves dependencies between calls: a call waits for the values it needs, while
independent calls can execute concurrently. Results can also be stored as Python objects in
program memory and reused across iterations. Do not impose one tool call per turn or hide
shared state in mutable globals; carry evolving state through typed parameters and returns so
the harness can see the dependencies. Files under `recurse.context().workspace` serve a different
purpose: durable artifacts available after the run.

### Tool registration and settings

Set `tools.source` to the application-relative tool file and register every public tool function
by its exact name. A `~` entry uses defaults. For the polygon example:

```yaml
tools:
  source: tools.py
  register:
    build_polygon: ~
    measure_area: ~
```

Use `tools.defaults` for shared settings and override individual registrations when needed:

- `storable` allows a return value to be saved in program memory; a non-`None` return type enables
  it by default. Disable it only for results used solely as context text or with no composable
  Python object. This storage is separate from workspace artifacts.
- `no_storage` lists parameters that must receive inline values instead of stored objects.
  It defaults to an empty list.
- `volatile` defaults to `false`. Set it to `true` when identical arguments can produce different
  results; repeated calls or recurring patterns then do not alert the harness to a stuck loop.
- `pure_args` lists parameters the tool guarantees not to mutate in place. It defaults to an
  empty list; `null` declares all parameters pure. The harness uses it to plan execution, so an
  incorrect declaration can cause races. Keep the conservative default when unsure.

`tools.built-in` defaults to `true` and controls built-in tools such as notes, TODO management,
and planning. Set per-tool options only when their behavior calls for them.

### Input and output contracts

Run inputs are declared as a JSON Schema (Draft 2020-12) under `inputs`. The resolved
`task` property — templated with `{{ input.NAME }}` placeholders — becomes the agent
task; the remaining values are available to tools at run time.

The successful result is declared as a self-contained object JSON Schema under `outputs`. Finish
the agent with only a JSON object matching that schema. Files written to the run workspace remain
separate downloadable artifacts.

## Designing a specialist loop

Choose the specialist's model in `agent.yaml`:

```yaml
agent:
  prompt: prompt.md
  model: gpt-6-astra
```

Omit `model` to use the service default, currently `gpt-5.6-luna`. Use the usual `recurse run`
or `recurse deploy --as mcp` command; there is no model CLI flag. Preparation prints the resolved
model. That selection stays with the prepared version even if the service default changes.
To change a deployed specialist's model, edit the declaration and deploy a new version.
Unsupported selections fail rather than falling back. Model usage is charged at the selected
model's rates, so the same token count can cost more with Astra.

Recurse is useful when an outer agent needs a reusable specialist for a domain with a checkable
outcome. The outer agent chooses the specialist's prompt, tools, inputs, and limits. The inner
specialist then works in a shorter loop: construct, measure, and revise until it succeeds or reaches
its declared search budget.

The practical decision is whether a bounded candidate-validator loop can operate through a small,
stable tool set without outer-agent supervision between attempts. A single substantial search can
justify a direct Recurse run. Expected reuse is the threshold for deploying the specialist as an
MCP, not for a direct run. A checkable result alone is insufficient: ordinary coding, editing, or
deterministic work is usually clearer when handled directly.

Start with the validator. A useful target has an independent oracle such as a test suite, scorer,
simulator, query executor, or human review gate. Work directly on one-shot tasks, independent
batches, deterministic workflows, or tasks without a checkable outcome.

Keep the tool set small and give each part one role:

- **Building tools** apply choices made by the specialist and reject malformed proposals. They may
  preserve basic invariants, but must not search for or reveal the answer.
- **Validator tools** measure a proposal with the independent oracle. Return the observed score,
  pass/fail state, and enough diagnostic evidence to inform the next attempt. Errors should name
  what the specialist can change before retrying.
- **Completion tool** decides whether the task is complete and, if so, preserves the best feasible
  candidate and returns an authoritative receipt. Register it alongside action and validator tools.
  The final JSON must match `outputs` and agree with the receipt. Artifacts hold durable evidence;
  they are not the only explanation of the result. A quality target may remain unmet when an
  agreed resource limit ends the search, but hard requirements still apply.

Carry evolving state through typed tool values and one stable agent storage key. Do not use mutable
module globals as run state. Keep the validator independent from candidate-producing tools, and do
not encode target-specific answers or deterministic solution workflows into the tool set.

Evaluate prompt or tool changes on repeated examples rather than one favorable run. Use
`recurse run` for one run-to-completion execution and `recurse deploy --as mcp` when the specialist
should become a reusable tool.

## Writing the agent prompt

Create the application prompt file and point `agent.prompt` to it in `agent.yaml`. Start from the
agreed proposal, the actual tools, and the verifiers. The prompt should tell the specialist:

- The outcome to improve, how it is measured, and how to choose between feasible results.
- Which requirements are hard constraints, which choices it can explore, and which domain facts
  constrain those choices.
- What each verifier measures, how the measurement relates to the goal, and what it does not
  establish. Include the domain context needed to interpret failures and tradeoffs.
- When to stop refinement, including acceptance conditions and the time or spending allowance.
- Which measured facts and artifacts to return, and how final JSON must agree with the completion
  receipt and the declared output schema.

For a lighter mounting bracket, describe the material, loads, strength, stiffness, and manufacturing
constraints. Explain how the checks establish feasibility and how mass ranks feasible designs.
Let the specialist explore geometry and construction choices; do not prescribe a sequence of CAD
edits or provide candidate answers. This leaves the decisions to the specialist while making the
objective and verification explicit.

Use `{{ input.NAME }}` in the declared task when caller inputs provide control values. Keep the
prompt focused on the task and decision-making; put concrete signatures and parameter instructions
in tool docstrings. Encourage hypotheses, alternatives, and refinement from observed measurements
rather than a fixed sequence of calls.

Revise the prompt when run evidence shows recurring misinterpretation, ignored constraints, or
unproductive exploration. Compare the revision with the baseline using the same cases, verifiers,
and allowances. A longer prompt or a successful single run is not evidence of improvement by itself.

## Outer-agent workflow

Before cloud work, propose the input/output contract, artifact formats, objective, independent
verification, hard constraints, and time/spending allowances. Explain why the approach fits and
what the custom agent will own. Ask about material ambiguities and state narrow routine assumptions.
Use arbitrary weights or thresholds sparingly; when needed, propose them with their tradeoffs.

1. Prefer an existing specialist when its contract already matches the task.
2. Establish a direct baseline. Consider independent approaches concurrently when allowances permit;
   sequence attempts that depend on earlier results. Explain the concurrency and isolation needed
   for the work and available resources, rather than choosing an arbitrary worker count.
3. Use `recurse run` on representative cases and record success, quality, trials, time, and cost.
4. Make small prompt, tool, or verifier changes supported by a hypothesis and observed evidence.
   Compare revisions as described below and repeat variable results.
5. Preserve the best feasible candidate when later attempts regress. One successful repair does
   not establish that the specialist generalizes.
6. Finalize using the acceptance target, justified diminishing returns, remaining allowance, or
   a concrete blocker. Save the best agent design and its measured facts.
7. Deploy as MCP only after evidence shows that the specialist is reusable.

### Comparing revisions fairly

A comparison needs the same cases, criteria, and allowances. Record success, quality, trials,
time, and cost so that a larger budget is not mistaken for a better design. Repeat variable
results. Prefer small changes supported by a concrete hypothesis over a larger redesign without
evidence that it is needed.

When a new kind of input exposes a missing check, strengthen the interpretation of the same user
intent while retaining earlier requirements. For example, a geometry check that captures a bag's
shape may overlook a shoe's laces. Version the revised check and evaluate both the baseline and
contenders with it. Retain old scores as history; do not rank them against scores from the new
check. Agree materially new requirements before changing what counts as acceptable.

Separate feasibility from quality. Every hard requirement must pass before a candidate can win
on the objective. Preserve the best feasible candidate when a later attempt has a higher score
but fails a requirement. If no candidates are feasible after repeated attempts, examine the tools
and verifiers instead of weakening the requirements to manufacture success.

### Finalizing the agent design

Finalizing the design means preserving the best prompt, tools, and verifiers supported by the
runs, not just choosing a candidate from one run. Use the user's acceptance target when provided.
Otherwise justify diminishing returns with the history of gains, remaining approaches, and the
cost of another attempt. An arbitrary count of low-gain attempts does not establish convergence,
and a runtime timeout does not establish diminishing returns or global optimality.

Track aggregate time and spending across runs. Do not begin an attempt that cannot fit the
remaining allowance. Before normal completion, save the best design and report its measurable
facts and the reason for stopping: acceptance, diminishing returns, a resource limit, or a blocker.
After abrupt failure, report only available evidence. Locate artifacts before claiming them,
and identify diagnostic files as diagnostics rather than final results.

## Building application artifacts

```python
import recurse

artifacts, record = recurse.build_bundle("path/to/app")
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

Carry evolving values between tools through typed arguments, not mutable module globals. Ask the
agent to save the first tool result under a stable key and pass that stored value into each later
call. Module globals are suitable for constants, but are not a supported persistence mechanism for
run state.

## Login

```sh
recurse login
```

Your browser opens the hosted Recurse login page; sign in with GitHub or Google. The
CLI listens on `http://127.0.0.1:8765/callback`, protects the flow with a PKCE
challenge and a single-use state value, and stores one opaque device credential in your
operating system keychain (service `recurse-cli`). Short-lived access tokens are obtained
when a command starts. The keychain entry is scoped to the active Recurse API URL, so TEST
and production logins can coexist. No credentials are written into application directories
or MCP configuration.

Log out when you want to revoke the device credential:

```sh
recurse logout
```

The local keychain entry is removed only after revocation succeeds, so a temporary
service failure can be retried safely.

## Runtime-secret management

Store an account-owned credential with two hidden prompts:

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

Bind a logical secret name to an environment name on a direct run or permanent MCP deployment:

```sh
recurse run path/to/app --secret GITHUB_TOKEN=github-token
recurse deploy path/to/app --as mcp --secret GITHUB_TOKEN=github-token
```

Repeat `--secret` for multiple bindings. A run keeps the secret versions that were active when it
started, so rotation affects future runs without changing one already in progress. Deleting a bound
secret disables affected MCP deployments and blocks future runs. Each `--secret ENV=NAME` binding
supplies the named secret as an environment variable inside tools. Keep the value itself out of
the manifest, command arguments, and application archive. If a required value is deleted before
use, the run ends with `secret_unavailable` before the tool executes.

## Direct runs

```sh
recurse run path/to/app --inputs inputs.json --cpu 1 --memory-mib 1024 \
  --secret GITHUB_TOKEN=github-token
```

The CLI builds and prepares an immutable version, admits that version directly, prints its run id,
and waits for terminal state. It does not create an MCP deployment. Runs have a 15-minute execution
limit. Inputs must be one JSON object;
omit `--inputs` for `{}`, or use `--inputs -` to read standard input. Resource limits use the same
ranges and defaults as deployment.

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
before cancelling it. If recovery also fails, the CLI prints the admission reference and warns that
the run's identity and state are unknown; keep that reference rather than blindly starting another
run. Recovery uses the existing finite HTTP retries and request timeouts. A second Ctrl-C stops
waiting for confirmation without claiming remote execution stopped.

Ctrl-C before admission stops local work cleanly; it does not promise that an already submitted
preparation was cancelled. Interrupting login closes its callback listener. Ctrl-Z retains native
terminal behavior: it suspends the local CLI, and `fg` or `bg` resumes it; remote work is not cancelled.
Ctrl-D still ends stdin input or aborts an unanswered prompt. Closing a terminal is not an explicit
run cancellation request.

Artifacts are available for 24 hours after completion. Downloads refuse unsafe paths and existing
files, verify size and SHA-256, and only then atomically move the file into place.

For automation, `recurse run` exits with `0` on success, `1` on agent failure, `2` on timeout,
`3` when it observes a remotely cancelled run, and `4` on infrastructure failure. A CLI interrupted
by Ctrl-C exits with `130`, including when cancellation is confirmed. Invalid command syntax exits
with `2` as well, so inspect the printed status and error rather than treating that code alone as
proof of a remote timeout. Other reported CLI errors exit with `1`.

Confirmed run failures include the run ID, status, a stable public error identifier and a short
explanation. For example:

```text
run: 77777777-7777-4777-8777-777777777777
status: failed
error: invalid_inputs: Run inputs do not match the agent's input schema. Check --inputs against agent.yaml.
artifacts: 0
```

| Public reason | Meaning and next step |
| --- | --- |
| `insufficient_balance` | Check `recurse billing balance`. Redeem an available credit code or open checkout with `recurse billing top-up 5`; an agent must obtain user approval before adding funds. Then retry. |
| `secret_unavailable` | A bound secret could not be supplied. Check `recurse secret list` and restore it with `recurse secret set NAME` if needed. |
| `invalid_inputs` | Check `--inputs` against the input schema in `agent.yaml`. |
| `invalid_agent` | Check the application declaration and packaged tool definitions. |
| `invalid_output` | Check the final return value against the declared output schema. |
| `execution_failed` | The agent failed; no more specific public cause is available. Keep the run ID when asking for help. |
| `artifact_failed` | Artifacts could not be collected or stored. Check their paths and retain the run ID. |
| `timed_out` | The service reports that the time limit was reached. Review the workload before starting another run. |
| `cancelled` | The service confirms cancellation. |
| `infrastructure_failed` | The service reports an infrastructure failure. Retain the run ID when asking for help. |
| `unknown_error` | A failed run has no recognized public reason. No private detail or guessed diagnosis is printed. |

Failure to observe a run is different from a failed run. `authentication_failed` directs you to
`recurse login`; `request_failed` means the CLI could not complete a service request, not that remote
execution stopped. `observation_timeout` means local polling ended without confirmation, not that
the service reported `timed_out`. These CLI failures exit `1`.

After an admitted run loses observation, the CLI retains its ID, warns that execution and charges
may continue, and prints `recurse status <run-id>` and `recurse cancel <run-id>`. Inspect the existing
run before starting another. If admission itself lost its response, keep the printed admission
reference: the CLI does not know whether a run was created and does not automatically resubmit it.
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
credential from the operating system keychain and obtains its own short-lived access token.
The device credential is never written to host configuration, environment variables, or
standard output. Each tool call is submitted once and can continue while the local process polls
for its result. A host cancellation cancels that call; temporary connection interruptions resume
waiting for the same call instead of submitting it again.

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
Adding funds is a separate purchase decision, not an automatic response to insufficient balance;
an agent must obtain user approval before purchasing credit.

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
print(record["source"]["sha256"], len(artifacts["source"]))
```

Run it directly with `recurse run examples/tiny-tuner --inputs inputs.json`, or deploy it with
`recurse deploy examples/tiny-tuner --as mcp`.
