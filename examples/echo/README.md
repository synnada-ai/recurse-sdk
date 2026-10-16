# Private compute-only echo

## When to use it

A minimal `tools.yaml` project with no prompt, model loop, provider keys or API calls. Python
returns its input message as JSON. The SDK validates and packages the source without importing it.

## How the loop works

The external agent builds this single-function project and inspects its stored receipt. The function itself has no agent loop.

## Run it

Connect using the [Tools quickstart](../../docs/tools-quickstart.md). After the user approves the
example $0.30 authorization, run from the SDK repository root:

```sh
recurse tools run examples/echo --function echo --inputs examples/echo/inputs.json \
  --idempotency-key my-first-echo --max-cost-micro-usd 300000
```

## Result

Expected output: `{"message":"Hello from my private tool"}`. Preparation and compute are charged;
zero manifest cost bases do not make this free. The cap is not a quote. Repeating the exact request
recovers its original receipt. Changing the message needs a new key. Use `recurse tools get CALL_ID`
to inspect progress and charges. 

## Limitations

This connection check does not demonstrate a useful API workflow. The example tests validate/build it and exercise its real Python
locally; live hosted acceptance is recorded separately.
