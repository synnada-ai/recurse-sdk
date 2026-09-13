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

Every public module-level function in the tool module must be registered under
`tools.register`, fully type-annotated, and carry a Google-style docstring whose `Args:`
section describes each parameter. Tools that return a value must describe it under
`Returns:`; tools returning `None` must not have a `Returns:` section. Tool parameters
and results may be scalars (`bool`, `int`, `float`, `str`, `None`), your own classes, or
tuples of your own classes; class instances are passed between tools by reference.

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
- **Result tools** preserve the best measured proposal, including when the target was not reached.
  Return a concise, authoritative receipt containing the facts the calling agent needs; artifacts
  are durable evidence, not a substitute for a readable result.

Carry evolving state through typed tool values and one stable agent storage key. Do not use mutable
module globals as run state. Keep the validator independent from candidate-producing tools, and do
not encode target-specific answers or deterministic solution workflows into the tool set.

Evaluate prompt or tool changes on repeated examples rather than one favorable run. Use
`recurse run` for one run-to-completion execution and `recurse deploy --as mcp` when the specialist
should become a reusable tool.

## Outer-agent workflow

1. Prefer an existing specialist when its contract already matches the task.
2. Name the candidate, independent validator, stopping rule, and smallest useful tool boundary. If
   any is unclear, work directly until the loop is understood.
3. Establish a direct baseline or unchanged fallback before shaping the specialist.
4. Use `recurse run` on a small representative cohort and record result quality, failures, time,
   and cost.
5. Make one supported change to the prompt, tools, diagnostics, or search limits in response to
   recurring evidence.
6. Retain the revision only when the same cohort supports its improvement. One favorable run is not
   sufficient evidence.
7. Deploy as MCP only after the loop is stable and likely to be reused.

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
secret disables affected MCP deployments and blocks future runs. Secret values are available only
to the application tools that declare them; they are never exposed during application preparation
or model calls. If a required value is deleted before use, the run ends with
`secret_unavailable` before the tool executes.

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
