# Recurse SDK

Author, package, and deploy Recurse applications.

Install the CLI as an isolated tool:

```sh
uv tool install recurse-sdk
```

Add the Python API to an application:

```sh
uv add recurse-sdk
```

The distribution ships two things:

- The `recurse` Python package: manifest validation, standards-based application packaging
  (`recurse.build_bundle`), and the runtime context tools use during a run
  (`recurse.context`).
- The `recurse` command: browser login, encrypted runtime-secret management, direct runs, MCP
  deployment, artifact retrieval, and billing.

## Quickstart

An application is a directory with an `agent.yaml` manifest, a system prompt, a tool
module, and a uv lockfile. See the runnable examples and `docs/guide.md` for the full authoring and
deployment guide:

- `examples/tiny-tuner` — tune a classifier against held-out validation F1.
- `examples/rna-fold-lab` — design RNA sequences against an independent forward-fold oracle.

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

For Codex, apply the startup and tool timeout settings shown in `docs/guide.md` after adding the
server.

## Documentation

- `docs/guide.md` — installation, authoring, runtime, login, deployment, local MCP access, and billing.
- `docs/reference/api.md` — generated API reference.
- `docs/reference/manifest.md` — generated `agent.yaml` reference.

## Contributing

Run `./check.sh` for formatting, lint, types, tests, generated-reference checks, and package builds.
Both the SDK and RNA example must reach 100% statement and branch coverage. The RNA example's
tests and dependencies live in `examples/rna-fold-lab/tests`; they are checked in their own environment.

Install the same gate before committing:

```sh
uv tool install pre-commit
pre-commit install
```

## License

Licensed under the [Apache License 2.0](LICENSE).
