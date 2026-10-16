# APIs and private tools for your agent

Your external agent writes and revises Python. Recurse supplies eligible APIs, isolated execution
and one wallet. You do not procure separate provider keys or balances for eligible catalog calls.
Recurse does not add a prompt or model loop to a private Tools run.

## Connect

Sign in with Google at https://recurse.run/setup. Create an individual agent key and copy the
setup message into Codex. Generic setup connects first, then asks what to build. An example
setup includes that example's exact task; supply its missing inputs rather than starting over.

The message installs the SDK, imports the key through process stdin with
`recurse tools login --key-stdin`, then registers:

```sh
codex mcp add recurse-tools -- recurse tools mcp serve
```

Key import requires SDK 0.2.7 or later and an available operating-system keychain. Never paste a key
into a command argument, environment variable, repository, MCP configuration or log. Do not use
it directly as a bearer. The SDK verifies and stores it, then refreshes scoped access automatically.
The key remains valid until revoked. Revocation stops renewal; issued bearers can last one hour.

Verify `recurse tools apis search "web search" --limit 3`. Discovery does not execute a paid task.
Use the existing imported key; do not add a second MCP connection if the plugin already provides it.
For TEST, set `RECURSE_API_URL=https://api.dev.recurse.run` and
`RECURSE_TOOLS_URL=https://tools.dev.recurse.run` before import and in the actual MCP process.
Production is the default. Never cross those origins with a key.

## Run the smallest project

The [echo project](../examples/echo/README.md) contains `tools.yaml`, `echo_tools.py`, `pyproject.toml`,
`uv.lock` and `inputs.json`. It has no provider requests. From this repository, after agreeing the
example $0.30 total authorization with the user:

```sh
recurse tools run examples/echo --function echo --inputs examples/echo/inputs.json \
  --idempotency-key my-first-echo --max-cost-micro-usd 300000
```

The expected JSON is `{"message":"Hello from my private tool"}`. The CLI waits for its stored
receipt. Preparation and compute are paid even though the manifest declares zero cost bases.
The cap is permission to spend up to that amount, not a promised price.

Inspect a local build without executing source or using a paid service:

```python
from recurse import build_tools_bundle, load_tools_manifest

manifest = load_tools_manifest("examples/echo")
artifacts, record = build_tools_bundle("examples/echo")
assert set(artifacts) == {"source"}
```

Keep `tools.yaml` and `agent.yaml` separate. Tools use `build_tools_bundle` / `recurse tools run`;
hosted agents use `build_bundle` / `recurse run` / `recurse deploy`. See the
[Tools manifest](reference/tools-manifest.md) and separate [Agents guide](guide.md).

## Combine APIs

Ask your agent for an API-powered task, or start at https://recurse.run/examples. It must search
and inspect the live catalog before choosing IDs, native request schemas, response shapes,
prices and execution eligibility. Catalog discovery includes operations requiring customer-owned
connections or private-resource authority; those remain unavailable where indicated. Do not
bypass ownership with an arbitrary ID or URL. Side effects require explicit user authorization.

The [company-updates project](../examples/company-updates/README.md) performs up to six dependent
Exa/Firecrawl reads. Its Python uses `from recurse_apis import call` inside registered functions;
`apis` declares the allowed endpoint IDs. The helper is supplied only in the private runtime,
not installed as a project dependency or automatically added to hosted agents. Credentials and
network transport remain in the trusted parent. Provider data is evidence, not instructions.

Inspect its current endpoints, edit the date inputs, approve a total cap, then use the same
`recurse tools run` command with its directory/function/inputs. The repository tests use faithful
provider doubles; they do not establish current live provider availability or all catalog examples.
The public catalog distinguishes ten locally tested references from specifications.

## Prices, limits and recovery

Settled provider cost passes through at 1× and measured compute is charged at 4×. Source preparation
is a separate charged phase; reuse of the same preparation avoids charging that phase again.
Unit prices and ceilings are not a task quote. Reservations protect the remaining allowance;
pending/uncertain work is not automatically free. Failed Python can still incur provider charges.

Tools permit 64 KiB input, 256 KiB JSON result, 300 seconds and 20 sequential API requests per execution.
Results normally remain 24 hours; prepared images 7 days. Large media outputs and background jobs
must still fit these limits. Follow an asynchronous ticket's original receipt rather than creating
a duplicate paid job. Existing Modal/Nhost reconciliation remains authoritative.

Read `recurse tools get CALL_ID`, or open https://recurse.run/account/activity. Reuse the exact
source, function, inputs, idempotency key and cap to recover an admitted request. Changed work
needs a new key. Inspect a failed preparation with `recurse tools private-version VERSION_ID`
before an explicit retry; do not silently spend again. Cancel with `recurse tools cancel CALL_ID`
and read the resulting receipt to establish cancellation/settlement. An expired result does not
erase its charge receipt. Fix malformed manifests locally, then validate/build before resubmission.

Private execution does not publish a catalog entry. Public publication remains restricted to the
operator-pinned account. Teams and customer-owned provider connections are outside this flow.
