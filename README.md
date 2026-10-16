# Recurse SDK

Give your external agent API access and private Python execution through one Recurse wallet.

## Installation

Python 3.14 or newer is required. Install Recurse from PyPI:

```sh
pip install recurse-sdk
```

This installs both the `recurse` command and the Python API. Check the installation:

```sh
recurse --help
```

Version 0.2.1 is required for account-funded runs on the current Engine. Version 0.2.0 submits a
900-second duration that the service no longer accepts. Upgrade the SDK before starting new runs.
Version 0.2.3 restores the 512 MiB minimum. SDK 0.2.2 rejected requests below 768 MiB locally.

If you prefer uv, install the CLI as an isolated tool:

```sh
uv tool install recurse-sdk
```

Add the Python API to an application:

```sh
uv add recurse-sdk
```

Running or deploying an application also requires [uv](https://docs.astral.sh/uv/getting-started/installation/)
on your PATH to build its package.

The distribution ships two things:

- The `recurse` Python package: manifest validation, standards-based application packaging
  (`recurse.build_bundle`), and the runtime context tools use during a run
  (`recurse.context`).
- The `recurse` command: browser login, encrypted runtime-secret management, direct runs, MCP
  deployment, artifact retrieval, and billing.

## Start with your own agent

1. Sign in with Google at [Setup](https://recurse.run/setup), then create an individual Tools key.
2. Install the current SDK and import the key through stdin with `recurse tools login --key-stdin`.
   Keep it out of command arguments, environment variables, project files and logs. The SDK stores
   it in your operating-system keychain. Key-based setup requires SDK 0.2.7 or newer.
3. Connect Codex using `codex mcp add recurse-tools -- recurse tools mcp serve`, then verify live API
   discovery. If you came from an example, retain its task; otherwise tell your agent what to build.
4. Agree missing inputs and a total spending cap before preparation. Your agent writes `tools.yaml`
   and Python, then runs privately. Setup/discovery itself does not execute paid work.

See the [Tools quickstart](docs/tools-quickstart.md), [manifest reference](docs/reference/tools-manifest.md)
and [canonical agent instructions](https://recurse.run/tools/SKILL.md).

| Project | Manifest | Builder | Execution |
| --- | --- | --- | --- |
| Direct Python tools | `tools.yaml` (`ToolService`) | `build_tools_bundle` | `recurse tools run` |
| Hosted Recurse agent | `agent.yaml` (`Agent`) | `build_bundle` | `recurse run` / `recurse deploy` |

Private Tools projects have no prompt or hosted model loop and do not require public publication.
Eligible APIs use Recurse's upstream credentials and wallet. Customer-owned account/private-resource
operations remain unavailable where live metadata says so. API costs pass through at 1× and compute
uses 4×; preparation is separately billable. A rate is not a quote for a complete task.

## Tools API

Recurse SDK 0.2.5 adds catalog access, direct tool calls, publisher-only publication, and a
separate local MCP bridge. Install or upgrade `recurse-sdk` to use these commands.

```sh
recurse login
recurse tools search
recurse tools inspect <tool-id>
recurse tools mcp serve
```

Python callers use `recurse.ToolsClient.from_login()` locally or supply an audience-scoped bearer
to `recurse.ToolsClient(access_token=...)` on a server. Calls require the inspected tool and price
versions, an idempotency key, and a maximum charge. See the
[Tools guide](docs/guide.md#tools-api) for invocation, publication, and authentication.

SDK 0.2.7 also accepts an individual Recurse Tools key from the account setup page.
`recurse tools login --key-stdin` reads it from a pipe, verifies it and saves it in the
operating-system keychain without opening a browser. Never pass a key as a command argument,
environment variable or MCP configuration value. `ToolsClient.from_login()` and the Tools MCP
bridge use the imported key and automatically refresh its one-hour Tools bearer.

After importing the key, connect Codex with:

```sh
codex mcp add recurse-tools -- recurse tools mcp serve
```

Give your agent the [Tools instructions](https://recurse.run/tools/SKILL.md). It can discover APIs,
build a private Python project and execute it through Recurse. The
[key setup guide](docs/guide.md#customer-key-setup) shows a hidden-input import and TEST origins.
`recurse tools logout` revokes only that imported key; ordinary `recurse login` and agent execution
keep their existing behavior.

Recurse SDK 0.2.6 also prepares and executes customer-owned `tools.yaml` projects privately.
Install or upgrade from PyPI as described above. These runs use compute billing and never
publish a catalog entry:

```sh
recurse tools run path/to/app-tools --function calculate --inputs inputs.json \
  --idempotency-key <stable-request-key> --max-cost-micro-usd 200000
```

The cap includes preparation and execution. The CLI waits for a receipt; Python's
`client.run(...)` returns after admission so callers can poll `get_call` themselves. See
[Private Tools runs](docs/guide.md#private-tools-runs) for retry and cancellation details.

SDK 0.2.6 adds approved API discovery (`recurse tools apis search` / `apis inspect`) and
per-function `apis` declarations for eligible metered requests through treg. Its MCP bridge forwards all thirteen
generic catalog, API discovery and private source operations. Remote source authoring requires
standard bundle construction and the returned scoped HTTP upload capability. The
[company-updates example](examples/company-updates/README.md) searches official company pages
and fetches bounded excerpts using one Recurse wallet.

## Hosted Recurse agents

An application is a directory with an `agent.yaml` manifest, a system prompt, a tool
module, and a uv lockfile. See the runnable examples and
[authoring and deployment guide](https://github.com/synnada-ai/recurse-sdk/blob/main/docs/guide.md):

- [examples/tiny-tuner](https://github.com/synnada-ai/recurse-sdk/blob/main/examples/tiny-tuner) — tune a classifier against held-out validation F1.
- [examples/predictive-modeler](https://github.com/synnada-ai/recurse-sdk/blob/main/examples/predictive-modeler) — train classification, regression, and forecasting pipelines against task-specific quality constraints.
- [examples/rna-fold-lab](https://github.com/synnada-ai/recurse-sdk/blob/main/examples/rna-fold-lab) — design RNA sequences against an independent forward-fold oracle.
- [examples/backpack-3d](https://github.com/synnada-ai/recurse-sdk/blob/main/examples/backpack-3d) — write, render, visually critique, and revise a 3D
  backpack using Astra and complete Python modeling programs.
- [examples/level-design](https://github.com/synnada-ai/recurse-sdk/blob/main/examples/level-design) — design puzzle-game levels against an independent solver:
  two runnable agents and one prompt-only example.

```sh
recurse login
recurse secret set github-token
recurse secret list
recurse run examples/tiny-tuner --inputs inputs.json --secret GITHUB_TOKEN=github-token
recurse deploy examples/tiny-tuner --as mcp --cpu 1 --memory-mib 1024 \
  --secret GITHUB_TOKEN=github-token
codex mcp add recurse -- recurse mcp serve <deployment-id>
claude mcp add recurse -- recurse mcp serve <deployment-id>
```

For Codex, apply the startup and tool timeout settings shown in the
[guide](https://github.com/synnada-ai/recurse-sdk/blob/main/docs/guide.md) after adding the server.

To connect all published gallery apps through one server, use `recurse mcp serve`
without a deployment ID. The shared connection exposes `apps.<gallery-slug>` tools;
private deployments still use their individual IDs. Runs use the caller's wallet.

## Documentation

- [docs/tools-quickstart.md](docs/tools-quickstart.md) — key setup, private Python, API access and recovery.
- [docs/reference/tools-manifest.md](docs/reference/tools-manifest.md) — `tools.yaml` authoring contract.
- [docs/guide.md](https://github.com/synnada-ai/recurse-sdk/blob/main/docs/guide.md) — installation, authoring, runtime, login, deployment, local MCP access, and billing.
- [docs/reference/api.md](https://github.com/synnada-ai/recurse-sdk/blob/main/docs/reference/api.md) — generated API reference.
- [docs/reference/manifest.md](https://github.com/synnada-ai/recurse-sdk/blob/main/docs/reference/manifest.md) — generated `agent.yaml` reference.

## Contributing

Run `./check.sh` for formatting, lint, types, tests, generated-reference checks, and package builds.
The SDK and examples must reach 100% statement and branch coverage. The RNA and backpack examples'
tests and dependencies live under their own `tests` directories and use separate locked environments.

Install the same gate before committing:

```sh
uv tool install pre-commit
pre-commit install
```

### Publishing

`pyproject.toml` sets `readme = "README.md"`, so package builds embed this README as the
PyPI description. Editing it on GitHub does not update an existing release:
[PyPI retains the metadata from that release's first upload](https://docs.pypi.org/api/json/).
To publish a corrected description with the next SDK release:

1. Choose an unpublished version, update `project.version` in `pyproject.toml`, and run `uv lock`.
   Update the SDK version reported in `src/_recurse_cli.py` and its matching CLI tests.
   Also run `uv lock --directory examples/rna-fold-lab/tests`,
   `uv lock --directory examples/backpack-3d/tests`, and
   `uv lock --directory examples/predictive-modeler/tests` to refresh their local SDK dependency.
2. Run `./check.sh` to validate the release and build its wheel and source distribution in `dist/`.
3. Inspect the wheel's `.dist-info/METADATA` and the source distribution's `PKG-INFO`.
   Their description must match the current README, with `pip install recurse-sdk` first and
   `uv tool install recurse-sdk` as an optional alternative.
4. With PyPI publishing access, upload only the two files for that version using
   `uv publish dist/recurse_sdk-<version>-py3-none-any.whl dist/recurse_sdk-<version>.tar.gz`,
   replacing `<version>` with the selected version.
5. Check the new release's description on PyPI and the default project page. In a fresh
   Python 3.14 environment outside this checkout, run `pip install recurse-sdk`, `pip check`,
   and `recurse --help`. Confirm `pip show recurse-sdk` reports the published version.

Version 0.1.0 retains its original description. Publish a new version to refresh the default
project page; [PyPI does not allow replacing previously uploaded files](https://pypi.org/help/#file-name-reuse).

## License

Licensed under the [Apache License 2.0](https://github.com/synnada-ai/recurse-sdk/blob/main/LICENSE).
