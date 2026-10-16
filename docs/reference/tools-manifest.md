# `tools.yaml` manifest reference

A tools service is a separate source distribution that runs registered Python functions directly.
It has no prompt, agent settings, model selection, or Agentia tool settings. Existing `agent.yaml`
applications continue to use `build_bundle`; tools services use `build_tools_bundle`.

```yaml
apiVersion: recurse.tools/v1alpha1
kind: ToolService
metadata:
  name: fixture-tools
  title: Fixture Tools
  summary: Returns JSON messages.
runtime:
  python: '3.14'
  lockfile: uv.lock
tools:
  source: tools.py
  register:
    echo:
      description: Return a message.
      input_schema:
        type: object
        properties:
          message:
            type: string
        required: [message]
        additionalProperties: false
      output_schema:
        type: object
        properties:
          message:
            type: string
        required: [message]
        additionalProperties: false
      pricing:
        provider_basis_micro_usd: 0
        compute_basis_micro_usd: 0
        provider_multiplier: 1
        compute_multiplier: 4
```

The manifest shape is closed: unknown fields and YAML aliases are rejected locally before paid
source preparation. Repeat shared schema text instead of using YAML anchors and aliases.
`metadata.name`, `title`, and `summary`
are required non-empty strings. `runtime.python` is `3.14`; `runtime.lockfile` is `uv.lock`.
`tools.source` is a canonical relative path to the source module. Registration names are the exact
public module-level Python function names, and every such function must be registered once.
Private helpers beginning with `_` do not need registration.

With SDK 0.2.6 or newer, each registration may also declare an `apis` list,
for example `apis: [exa.web.search, firecrawl.web.scrape]`. It contains at most 20 unique endpoint
IDs, each 1–128 characters from ASCII letters, digits, underscore, dot and hyphen, beginning
with a letter or digit. Omitted or
empty grants no API access. The service freezes eligible endpoint contracts and prices
during private preparation; an author cannot provide credentials or override the rate through
this declaration. See the [private API guide](../guide.md#eligible-apis-in-private-tools).

Each registration requires a non-empty `description`, `input_schema`, `output_schema`, and
`pricing`. Both schemas use Draft 2020-12 and must be self-contained: local references are
permitted; external references are rejected. Values must be JSON-compatible.

Inputs declare `type: object`, `properties`, and `additionalProperties: false`. The properties
must match the named function parameters exactly. Positional-only parameters and variadic `*args`
or `**kwargs` are rejected. Ordinary positional parameters and keyword-only parameters work;
synchronous and asynchronous functions work. Required properties must be exactly the parameters
without Python defaults. Defaults must be literal JSON values that satisfy their parameter schema.
If an optional property's schema declares a default, it must match the Python default. Required
properties must not declare defaults. Functions retain type annotations and Google-style docstrings
with parameter and return descriptions, just as agent tool source does.

Both bases must be zero, the provider multiplier must be `1`, and the compute multiplier must
be `4`. These are source declarations, not a free-execution quote. The trusted service charges
actual settled provider cost at 1× and measured compute at 4×, including separately billed source
preparation. An empty `apis` list means no provider requests, not free compute. The caller must
approve a total allowance before preparation; execution still follows holds and settlement.

## File and command selection

Keep plural `tools.yaml` separate from `agent.yaml`; there is no singular `tool.yaml` alias.
`tools.yaml` uses `recurse.tools/v1alpha1`, `ToolService` and `build_tools_bundle`;
`agent.yaml` uses `recurse.run/v1alpha1`, `Agent` and `build_bundle`. Direct private execution is
`recurse tools run DIRECTORY --function NAME --inputs inputs.json --idempotency-key KEY
--max-cost-micro-usd CAP`; hosted agent execution remains `recurse run`/`recurse deploy`.
Tools support starts in SDK 0.2.5, private execution/API declarations in 0.2.6, and Tools-key
stdin import in 0.2.7. Install the current SDK for the complete customer workflow.

The [echo example](../../examples/echo/README.md) is a minimal compute-only project; the
[company-updates example](../../examples/company-updates/README.md) composes metered search and
extraction. Both validate/build without importing source. Inspect live schemas and eligibility
before using an API; no fixed provider inventory in this document grants execution access.

Tools are bounded by 64 KiB input, 256 KiB JSON output, 300 seconds and 20 sequential API calls per
execution. Results normally remain 24 hours and prepared images 7 days. A failed or cancelled
operation may already have incurred cost; reconcile the original receipt before submitting again.
These are Tools limits, not the separate hosted-agent limits. The `recurse_apis` helper is supplied
only in private Tools execution and is not automatically added to the hosted-agent harness.

The project must declare a standard build backend in `pyproject.toml`. Its sdist must contain
`tools.yaml`, the declared source module, `pyproject.toml`, `PKG-INFO`, and the exact `uv.lock`.
The backend controls which additional files enter the source distribution. The builder parses
source with Python's AST and never imports or executes it on the author's machine.

The returned record uses `apiVersion: recurse.tools.application/v1alpha1`, distinct from the agent
artifact contract, and records the source distribution's SHA-256 and byte size. See the
[Python API reference](api.md) for `load_tools_manifest` and `build_tools_bundle`.
