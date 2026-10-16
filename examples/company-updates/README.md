# Company product-update evidence

This private Python project requires Recurse SDK 0.2.7 or newer and an enabled account with
sufficient wallet funds. It also supports the remote MCP source workflow in the SDK guide.

## When to use it

Give an external agent linked evidence for comparing recent official updates from Linear,
Notion and ClickUp. The project runs Python privately through Recurse using one account and
wallet. The customer creates no Exa, Firecrawl or treg credential.

## How the loop works

The external agent selects an explicit publication-date window and runs `company_updates`.
For each company, Python searches its official domain with at most three results, selects a
distinct public HTTPS HTML page, and sends that returned URL to Firecrawl. It returns up to
1,200 characters of extracted text, the source URL and the publication date supplied by search.
Missing dates stay null, and missing results/content carry explicit reasons. A returned target
HTTP error (metadata status 400 or above) is marked `target_error`, even if it has Markdown.
The external agent
interprets the evidence and can revise the Python or dates and submit a new run.

The manifest declares only `exa.web.search` and `firecrawl.web.scrape`. The service freezes their
approved contracts and rates during preparation. `from recurse_apis import call` is supplied by
the isolated execution runtime; it is not a customer credential or a project dependency.
The trusted parent makes provider requests. Project code has no unrestricted network access.

## Run it

Create a Tools key at https://recurse.run/setup. Supply it to the import command through process stdin; never put it in arguments, source or logs. Install the SDK and inspect current production metadata before spending:

```sh
pip install --upgrade "recurse-sdk>=0.2.7"
export RECURSE_API_URL=https://api.recurse.run
export RECURSE_TOOLS_URL=https://tools.recurse.run
recurse tools login --key-stdin
recurse tools apis search search
recurse tools apis inspect exa.web.search
recurse tools apis inspect firecrawl.web.scrape
```

Update the example `inputs.json` date window, then run from the SDK repository root:

```sh
recurse tools run examples/company-updates --function company_updates \
  --inputs examples/company-updates/inputs.json \
  --idempotency-key <new-stable-request-key> --max-cost-micro-usd 300000
```

The $0.30 cap includes preparation, execution and at most six paid requests. It is a maximum
authorization, not a promised charge. Actual provider debits are passed through at 1×, and
compute is charged at 4×. Inspect current metadata before authorizing spending.

Repeat unchanged source, inputs, cap and key to recover an already admitted call. Source/input
changes need a new key. Inspect a failed preparation before explicitly retrying because it may
already have incurred compute charges. Use `recurse tools get <call-id>` and `cancel <call-id>`
for stored status or cancellation.

## Result

The JSON includes the requested date window and one row per company. Each row has either a
source URL, title, nullable provider date and short excerpt, or a missing-data reason. A supplied
URL is fetched from that search response; the project never substitutes a hard-coded article or
invents a publication date. API request errors fail the run and remain visible in its receipt.

## Limitations

This is bounded evidence collection, not a complete market analysis or an LLM-generated summary.
Publication-date filters depend on the provider's index. The code does not establish that a
provider date is accurate or that a page contains the latest announcement. The caller owns that
interpretation. A returned target error page can be billable. Firecrawl is requested only for
Markdown with no PDF parser, actions, crawling, extraction add-ons or fallback requests.

The platform currently allows at most 20 API requests per execution, 64 KiB inputs, 256 KiB
responses/results and five minutes of execution. This example makes no more than six requests.
Its runtime helper is intentionally unavailable for direct local execution; automated tests
inject a faithful helper double and make no real provider calls.
