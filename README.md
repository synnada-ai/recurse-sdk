# Recurse SDK

Author, package, and deploy Recurse applications.

## Installation

Python 3.14 or newer is required. Install Recurse from PyPI:

```sh
pip install recurse-sdk
```

This installs both the `recurse` command and the Python API. Check the installation:

```sh
recurse --help
```

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

## Quickstart

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

## Documentation

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
