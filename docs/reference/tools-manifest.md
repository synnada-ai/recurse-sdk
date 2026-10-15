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

On the Stage 41b development branch, each registration may also declare an `apis` list,
for example `apis: [exa.web.search, firecrawl.web.scrape]`. It contains at most 20 unique endpoint
IDs, each 1–128 characters from ASCII letters, digits, underscore, dot and hyphen, beginning
with a letter or digit. Omitted or
empty grants no API access. The service freezes approved read endpoint contracts and prices
during private preparation; an author cannot provide credentials or override the rate through
this declaration. These capabilities are undergoing TEST validation and are not in a published
SDK release yet. See the [private API guide](../guide.md#approved-read-apis-in-private-tools).

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

M1 permits only explicitly zero-priced fixture definitions. Both bases must be zero, the provider
multiplier must be `1`, and the compute multiplier must be `4`. These multipliers record the initial
paid pricing policy; the fixture remains free. Paid rates, measured compute, wallet holds, and
settlement are introduced in M3.

The project must declare a standard build backend in `pyproject.toml`. Its sdist must contain
`tools.yaml`, the declared source module, `pyproject.toml`, `PKG-INFO`, and the exact `uv.lock`.
The backend controls which additional files enter the source distribution. The builder parses
source with Python's AST and never imports or executes it on the author's machine.

The returned record uses `apiVersion: recurse.tools.application/v1alpha1`, distinct from the agent
artifact contract, and records the source distribution's SHA-256 and byte size. See the
[Python API reference](api.md) for `load_tools_manifest` and `build_tools_bundle`.
