# Autonomous-agent validation

Latest status: the retry batch completed; see **Retry after reported infrastructure fix** below.
The following first-attempt record is retained as history.

The user authorized this batch on September 15, 2026. Two runs were attempted; both ended in
`infrastructure_failed` without agent output or artifacts. The remaining ten were not launched
because the failure repeated. Local tests exercise the tools with supplied interpretations and
fixed candidate probes; they do not establish LLM understanding or search quality.

## Execution evidence

Both attempts used source commit `5d19a87d7543c0a8f528cfbe9de348b596c5e34e`, the service-selected
`gpt-5.6-luna` model, one CPU, and 2 GiB memory. Runtime preparation completed before admission.

| Input | Run ID | Outcome |
| --- | --- | --- |
| Inconsistent binary | `3fad7998-f64b-4fe3-8f43-aa15709f8cc6` | Infrastructure failure; no output/artifacts |
| Valid binary | `cf0b9fcf-cc5b-4736-bba1-8df531c30821` | Infrastructure failure; no output/artifacts |

The public status endpoint confirmed both terminal failures but supplied no underlying cause or
agent-version identifier. No task-interpretation, consistency, experiment, or model-reload check
can be scored from these runs. No claim is made that training did or did not execute internally.

The account balance changed from **$102.086276** to **$102.067010** after the first attempt, then
to **$102.065671** after the second: an observed decrease of **$0.020605**. These are account
balance deltas, not itemized run charges. No funds were added. Public status snapshots are retained
locally under the ignored `.baseline-results/cloud/` directory; there were no artifacts to fetch.

The PR remains a draft. Resolve the service infrastructure failure before resuming the batch;
retain these attempts as failures, and record any replacement runs separately.

## Batch

1. Run each of the six files under `../inputs/` once. Each allows at most eight fits and 600
   cumulative training seconds. Use the platform's default agent model and 2 GiB runtime memory.
2. Run each of the six files under `inconsistent-inputs/` once. The original prose is unchanged,
   while structured quality contradicts its metric, averaging, or seasonal-period requirement.
   These must stop as `inconsistent_inputs` before data inspection or any model training.

This is twelve remote runs. The training ceiling does not cap LLM charges or the full run's cost;
inspect actual usage between runs. The approved batch uses existing credits; no top-ups or
unlimited repeats are authorized.

## Review evidence

For valid inputs, inspect the review, resolved contract, full trial history, final receipt,
evaluation report, and model bundle. Verify appropriate features and splits, real hypotheses
responding to measured evidence, every constraint retained, best feasible selection, successful
artifact reload, and honest stop reasons. `no_feasible_model` is acceptable when supported by the
measurements; a tool or interpretation error is not evidence of infeasibility.

For inconsistent inputs, verify the conflict explanation points to the actual disagreement,
there are no training trials, and no resolved evaluation contract or accepted model artifact.

Record run IDs, agent version/model, costs, failures, and artifact locations. Revisit prompt or
tools when evidence warrants it and track replacement runs against the authorized scope and
remaining allowance. Keep local tool-chain results distinct from autonomous-agent outcomes.

## Retry after reported infrastructure fix

The user authorized another attempt, then requested that work stop after all launched runs
finished so they could investigate the backend. All twelve cases reached a terminal state.
Runtime source was unchanged (repository commit `6903176`; its change was documentation only).
All runs used `gpt-5.6-luna`, one CPU, and 2 GiB memory.

| Case | Run ID | Result |
| --- | --- | --- |
| inconsistent-panel-forecast | `f2072869-5024-449b-bdae-08b72ae9c52c` | `inconsistent_inputs` |
| valid-forecast | `7c0ba37d-374b-47d2-a073-3af5a5f2003f` | `execution_failed` |
| valid-multilabel | `5d2e850f-b2db-430d-bcd9-2d0f95f6fb80` | `unsupported_task` |
| valid-panel-forecast | `5221feeb-354c-4507-a69e-7a5ea2cbf401` | `unsupported_task` |
| inconsistent-binary | `c7dc6841-af1b-4acf-8f7c-9f7988f782c0` | `inconsistent_inputs` |
| inconsistent-forecast | `8c6d54ad-bed7-4650-8ab0-c7a2e25735f2` | `execution_failed` |
| inconsistent-multiclass | `acd71dd0-8082-4cd1-bbfb-260ed18aae25` | `inconsistent_inputs` |
| inconsistent-multilabel | `0b3137c2-1122-4318-a58b-46ab05756ceb` | `inconsistent_inputs` |
| inconsistent-regression | `9ebe44a8-8755-419d-9f07-bb26d2665d03` | `inconsistent_inputs` |
| valid-multiclass | `baa49d44-3b55-464b-aa10-6d2ccbdb5775` | `execution_failed` |
| valid-binary | `7e9da036-77c7-4cc9-8f98-a9f9b08a908d` | `execution_failed` |
| valid-regression | `511a873b-7fbd-4696-9c13-f66d4fc73689` | `execution_failed` |

Five contradictory requests returned `inconsistent_inputs`. Their reports identify the actual
metric disagreements. All five retrieved review databases confirm no dataset or trial state. The forecast contradiction
failed. Both valid-task receipts confirm no frozen contract or training trials.

No valid task produced a trained model. Binary, multiclass, regression, and single-series
forecasting ended `execution_failed`, with null output and no artifacts. Multilabel and panel
forecasting returned `unsupported_task`: the agents reported that `resolve_problem` rejected
literal specifications through its storage-reference argument interface. These are tool-interface
failures, not evidence that these prediction tasks are unsupported. Backend traces are needed
to verify the reported cause; the public service exposes no diagnostic trace.

Additional finding: successful rejection summaries correctly compare prose and structured quality,
but recorded `task_quality` values copied supplied quality rather than independently extracting
the prose. The regression extraction was also incorrectly unwrapped. No runtime changes or further
runs were made after the user asked to stop.

Four CLI launches failed authentication rate limiting before admission (no run ID). Three were
retried; the panel contradiction encountered the rate limit twice. The last four cases reused one
authenticated preparation. Those four share version ID `0da700ff-e8cd-4eba-9b87-b57b5f38bc47`.
Public status snapshots, receipts, reports, and SQLite evidence are retained under the ignored
`.baseline-results/cloud/` directory. The PR remains a draft pending backend diagnosis and complete
autonomous validation.

Retry balance: **$102.065671 → $100.758708**, an observed account decrease of **$1.306963**.
Including the original two infrastructure failures, the total observed decrease is **$1.327568**.
These are account balance deltas, not itemized run charges. No funds were added.

## Local improvements after the retry

The user authorized improvements from this evidence and explicitly paused further cloud execution
until they confirm the backend diagnostic issue is fixed. No new cloud runs have been launched.

- `no_storage` now declares review quality/conflict/question fields, problem specifications, and
  candidate configurations as inline JSON inputs. Previously those agent-authored objects had no
  upstream producer, which is consistent with the reported storage-reference failures. The packaged
  manifest is tested locally; actual backend argument handling remains unverified.
- The review gate rejects malformed quality wrappers, nested container shapes, and unexpected
  fields before freezing the interpretation. The prompt illustrates independent prose extraction.
  This does not prove semantic extraction accuracy; that still needs autonomous validation.
- `tool_error` is an explicit receipt status/stop reason for a blocking tool failure. It preserves
  diagnostics and trial history without claiming the prediction task is unsupported or infeasible.
  It cannot recover diagnostics from a runtime termination that prevents finalization.
- Every finalized run now exposes `review.json` for direct inspection of the extraction and conflicts.

The original run records above remain unchanged evidence from the earlier agent version.

Local verification of this revision: **145 modeler unit tests at 100% statement/branch coverage**,
**six full real-data tool-chain integrations**, and **434 SDK tests at 100% coverage** passed.
Strict typing, lint, docstrings, packaged manifest checks, and SDK builds also passed.
These are local checks; the new inline interface and extraction behavior await cloud validation.

The example and installed operational CLI have subsequently been upgraded to `recurse-sdk==0.1.4`.
Future remote operations should use the upgraded installed `recurse` command; `uv run recurse`
from this SDK source checkout still invokes its editable development version. Cloud runs remain
paused until the user confirms the backend fix.
