# Autonomous-agent validation

Latest status: all six task types have cloud-trained bundles with reproduced validation/final
scores, and all six contradictory tasks reject before loading data. Panel evidence uses the
current three-trial request; its earlier eight-trial run timed out. See **SDK 0.1.7 engine
verification** and **Panel source and workload verification** below. Earlier attempts remain
as history, including failures and their different allowances.

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

## Verification after the next backend fix

The user reported another backend fix and authorized verification. One previously failing valid
regression case was run using SDK 0.1.4 and agent source commit `9f50307`:

- Run: `dd7e5957-9ddf-4ac9-b838-07feca807a5e` (`gpt-5.6-luna`, one CPU, 2 GiB).
- Service status: `succeeded`; authoritative agent receipt: `tool_error`.
- The review correctly extracted the prose objective as minimize MAE. Dataset inspection completed.
- The agent reported that `resolve_problem` rejected literal specification values and required
  storage references, including for the scalar `kind`, despite the `no_storage: [specification]`
  declaration. It reported trying to omit optional null fields and use available references.
  This is the agent's diagnostic; backend traces are still needed to establish the cause.
- Retrieved SQLite state contains only `review`, `dataset`, and `receipt`; no contract and zero
  trials. The public receipt agrees with SQLite. All six artifacts were retrieved under the ignored
  `.baseline-results/cloud/backend-fix-regression/` directory.

This attempt returned useful diagnostic artifacts instead of an opaque execution failure, but it
produced no model. It does not establish that all backend failures are fixed. No further cases were
launched. Observed balance change: **$100.758708 → $100.520942** (decrease **$0.237766**).

## SDK 0.1.6 upgrade and retry

At the user's request, the installed CLI and example dependency were upgraded to **0.1.6**, then
valid regression was retried as `9a4a6356-db20-447f-97b4-6bd1f25a499d` using the same input,
`gpt-5.6-luna`, one CPU, and 2 GiB memory. Local tests against the published SDK passed all 145
modeler cases with 100% statement/branch coverage; 434 SDK tests, lint, formatting, and types passed.

The service returned `succeeded`, but the authoritative agent receipt is **`tool_error`**. The
agent again reported that `resolve_problem` requires general-storage references for specification
fields and rejects inline substitutions; it also reported that notes were not valid storage keys.
This remains an agent-reported diagnostic requiring backend trace confirmation.

All six artifacts were retrieved under `.baseline-results/cloud/sdk016-regression/`. SQLite state
confirms successful review and dataset inspection, no frozen contract, and zero trials. The saved
receipt agrees with public output. No model was trained. No further remote attempts were launched.
Observed account balance change: **$100.520942 → $100.245124**, a decrease of **$0.275818**.

## Tool-authoring root cause reproduced locally

Inspection of Agentia commit `10283b65fcd5948ea5d11344b1f76a8f05dd2416` identified an error
in this example's signatures. Under strict schema generation, `dict[str, Any]` maps every value
in the dictionary to a mandatory storage reference. `no_storage` only suppresses the parameter's
outer optional reference wrapper. It does not override nested `Any` semantics. A minimal old
resolver signature rejects all four literal fields in a forecast specification in this actual
schema generator, matching the agent's reports. The earlier assumption that the manifest setting
alone would permit arbitrary JSON was incorrect.

The input types now use concrete scalar/list unions and typed dictionaries. Ten opt-in tests
exercise Agentia's real strict schemas and JSON conversion for all six task specifications/reviews
and representative configurations. They pass with no mandatory substitutions. The default unit
suite also prevents `Any` from returning anywhere in registered tool input annotations.
Local results: 146 modeler tests at 100% statement/branch coverage, 434 SDK tests at 100%,
strict types, lint, and docstrings pass. These checks reproduce and address the authoring failure;
cloud verification follows separately.

## Corrected tool types verified in cloud

Run `3c4debd5-6615-4b9c-aa31-2b465c029de9` with SDK 0.1.6 successfully froze the regression
contract and attempted three fits. This confirms that the concrete tool types resolve the
storage-reference blocker. All eight artifacts were downloaded to
`.baseline-results/cloud/typed-input-regression/`.

All three workers failed with `ModuleNotFoundError: No module named 'joblib'`, although the parent
tool module imports that package successfully. The service returned `succeeded`; the authoritative
receipt was `tool_error`. No trained model was accepted.

A regression test reproduces this dependency visibility gap by disabling automatic site-package
loading in the child interpreter. It fails with the same missing-joblib traceback before the fix.
Workers now inherit the parent's effective import paths through `PYTHONPATH`; the test then trains
and evaluates successfully. All 147 modeler unit tests pass with 100% statement/branch coverage.
This subprocess fix still requires cloud verification.

## Worker dependency fix verified, September 16

At source `3d9ef40`, regression run `85c681b7-6ce3-493f-bb9b-2a5d3fd3c789` trained and
evaluated six candidates successfully. Extra-trees trial 5 won with validation MAE
3.0947839722 MPa and final-test MAE 3.5631818597 MPa. Reloading the downloaded model reproduced
both scores on the checksummed Concrete dataset and saved splits. Total fitting time was
15.104 seconds; the baseline validation MAE was 14.1465 MPa. This confirms the dependency fix.

All six inconsistent-input cases returned `inconsistent_inputs`; their downloaded SQLite
states contain only review/receipt metadata and zero trials, confirming rejection before dataset
loading. Binary and panel-forecast public responses nevertheless paraphrased the saved receipts
and inserted empty optional artifact paths. The saved receipts themselves were correct.
The output schema now rejects empty artifact paths, and the prompt/completion description explicitly
require preserving every receipt field. A regression test failed before this change; all 148
modeler tests pass afterward at 100% statement/branch coverage, with strict types and lint passing.
The two affected cases are rechecked separately in the results below.

## September 16 validation results

The twelve-case batch used source `3d9ef40`, SDK 0.1.6, `gpt-5.6-luna`, one CPU and 2 GiB per
isolated runtime. Criteria, splits and budgets were unchanged: up to eight fits and 600 cumulative
training seconds, within the platform's 15-minute run limit. These are single-run observations,
not repeated estimates of agent reliability or evidence of globally optimal models.

### Modeling

| Task | Run ID | Outcome |
| --- | --- | --- |
| Regression | `85c681b7-6ce3-493f-bb9b-2a5d3fd3c789` | Accepted extra-trees bundle; six evaluated trials |
| Multilabel | `aca67d89-6cac-41d2-894e-80499c8a1b83` | Accepted linear text bundle; seven evaluated trials |
| Single-series forecast | `0cc673e7-bc3e-43dd-9da7-96b599ced094` | Accepted weekly seasonal bundle; five evaluated trials |
| Binary | `fe19e842-ffc1-4210-ad6e-02aa84d35daa` | Infrastructure failure storing/confirming output files |
| Multiclass | `3fd1b90e-2e56-4634-a1f6-cd934d9f81fb` | Infrastructure failure storing/confirming output files |
| Panel forecast | `0f583761-735e-4b7d-af19-c78157439010` | Infrastructure failure authorizing/recording model usage |

Each of the three accepted bundles was downloaded and reloaded locally. On the checksummed source
datasets and saved, disjoint splits, both validation and final-test scores reproduced to 1e-9
tolerance. Trial histories confirm selection of the best feasible evaluated candidate.

| Task | Validation | Final test | Cumulative training |
| --- | --- | --- | --- |
| Regression, MAE (MPa) | 3.094784 | 3.563182 | 15.104 s |
| Multilabel, micro-F1 | 0.548226 | 0.552222 | 35.344 s |
| Daily forecast, MAE (rentals) | 916.000000 | 2522.428571 | 8.495 s |

Regression improved substantially over baseline and linear models, then compared tree depth/leaf
settings. Multilabel compared threshold/regularization/text settings; threshold 0.15 regressed from
the selected 0.2. Forecasting compared seasonal, linear and tree alternatives and preserved the
weekly baseline. Its much higher final-test error limits claims of forecasting quality: completing
the contract does not establish a strong forecast. No thresholds were weakened or added.

For binary and multiclass, status lists seven and four artifacts respectively, but **every listed
artifact grant returned `artifact was not found`** when checked individually. No model result can
be established. Panel forecasting returned no artifacts. The public errors say tools may already
have executed and that the usage error does not establish insufficient balance. These runs were
not repeated; backend recovery is needed. Raw statuses, per-file errors and recovered artifacts
are retained under the ignored `.baseline-results/cloud/worker-fixed-*` paths.

### Consistency gate

| Contradictory task | Run ID |
| --- | --- |
| Binary | `cc0b4fa3-1821-44f1-818a-3dec39fdd65d` |
| Multiclass | `95ab1c93-7afa-4a7b-b020-306fadec3779` |
| Multilabel | `c82d0498-14e7-4ae6-be9f-e34a56732b14` |
| Regression | `cf3e2b40-5122-48dd-ae43-30f5e33ecfd1` |
| Forecast | `ce255c97-239b-440a-bf2e-4cdb7160c9ef` |
| Panel forecast | `23b3b5b4-deb8-4c5e-ba68-839b8374303d` |

All six returned `inconsistent_inputs`. Downloaded states have only review/receipt metadata and
zero trials, establishing rejection before dataset loading. Binary and panel final-response
fidelity failed despite correct saved receipts, motivating source `1a8e02a` described above.

The corrected binary repeat `6d173bfa-7107-422e-a105-d39a39fe0666` returned exactly its saved
receipt, with no empty artifact paths. Corrected panel repeat
`1ee7b39b-4f46-4480-858d-253b11ae4c40` also exactly matches its saved receipt. Both downloaded
states confirm rejection before dataset loading or training. One panel preparation failed
authentication before admission; the replacement was started only after fresh authenticated
requests succeeded. No duplicate admitted run was created.

### Checks, usage and stopping point

The final full `check.sh` passes: 504 SDK, 21 RNA, 29 backpack and 148 modeler tests, each suite
at 100% statement/branch coverage, plus lint, strict types and source/wheel builds. CI is green.
The output fix does not change search, metrics, data access or training allowances. Existing
resource bounds and scoped cleanup remain in place; no new dependencies or coverage exemptions.

Account balance changed from **$100.055305 to $97.045187**, an observed decrease of **$3.010118**.
The final available balance equals the total. These are account-wide deltas, not itemized run
charges. No funds were added. Fourteen runs were admitted (the twelve-case suite and two targeted
receipt repeats); all are terminal. No model-run retries were launched after the backend failures.

Stop reason: **backend blocker** for binary, multiclass and panel autonomous validation.
The current agent design is saved in PR #19. Local functionality across all six tasks is verified,
but the three failed remote cases cannot be counted as accepted autonomous models. Recover their
outputs or address the artifact/usage failures before resuming those cases. Three accepted runs
and two receipt repeats are useful evidence, not a claim of universal reliability.

## SDK 0.1.7 compatibility update

The installed Recurse CLI and agent runtime were upgraded from 0.1.6 to 0.1.7 at the user's
request, with the example lockfile refreshed. All 148 modeler tests pass against the published
0.1.7 package at 100% statement/branch coverage. The cloud results above remain observations
from SDK 0.1.6; this dependency upgrade does not establish resolution of the backend blockers.

## SDK 0.1.7 engine verification

After the user reported an engine fix, the three blocked cases were retried at source `319107e`,
SDK 0.1.7, with unchanged inputs, criteria and budgets. One multiclass preparation returned HTTP
502 before admission; one replacement was admitted. Starting account balance: $97.045187.

| Task | Run ID | Outcome |
| --- | --- | --- |
| Binary | `aa86ee61-c068-4cd4-960b-2fc6ecccf285` | Accepted extra-trees bundle; seven evaluated trials |
| Multiclass | `ba1e3f14-36f5-4bba-90b6-a08663226037` | Accepted linear bundle; six evaluated trials |
| Panel forecast | `dba45a8d-8cc3-4cd0-8c49-0877f092c329` | Service succeeded; receipt `tool_error`, dataset inspection timed out twice |

Both accepted bundles and all their artifacts downloaded successfully. Reloading on original
checksummed data and saved splits reproduced validation and final scores. Resolved contracts retain
the requested targets/features/quality: duration excluded for binary, yes as the positive label,
recall >= 0.5 preserved, and all 16 features with macro-F1 for multiclass.

| Task | Validation | Final test | Cumulative training |
| --- | --- | --- | --- |
| Binary precision / recall | 0.472250 / 0.504310 | 0.495902 / 0.521552 | 42.237 s |
| Multiclass macro-F1 | 0.940715 | 0.932185 | 15.721 s |

Binary compared baseline, linear and tree families, then thresholds and tree complexity, preserving
the best feasible model when higher precision violated recall. Multiclass compared families and
regularization strengths. These are evidence of working search and constraint handling, not global
optimality or repeated reliability measurements.

Panel's six diagnostic artifacts downloaded. It stopped before freezing a contract or training
because `inspect_dataset` timed out twice after 30 seconds. An independent GET of the same Zenodo
archive returned HTTP 504 locally after 30.6 seconds. The prior usage-authorization failure did
not recur. The example now bundles the previously verified, unmodified 199,791-byte Tourism source
archive with publisher attribution and license links. It retains the original checksum, all 366
series/109,280 observations, and unchanged splits/metrics. Regression tests verify offline loading,
archive inclusion/attribution, and rejection of tampered bytes. The focused panel retry follows.

## Panel source and workload verification

The bundled-source retry at `4d6f459`, run `36a852de-5c49-4222-9552-9b6403400378`, kept the
original eight-trial/600-training-second request. It reached the platform's 15-minute limit with
no artifacts or final output. That establishes a run-limit failure, not which internal step
caused it. A representative local 100-tree panel fit took 18.631 seconds; this does not establish
cloud timing. No acceptance is claimed for the original eight-trial request.

A diagnostic changed **only max_trials from eight to three**, retaining the same task, dataset,
600-second training allowance, CPU/memory ceilings, objective, splits and metrics. Run
`489dd805-83e7-4b00-a183-3833cab0d14a` succeeded, evaluated all three fits, and returned ten
downloadable artifacts. Its final response exactly matches the saved receipt.

| Trial | Approach | Validation MASE |
| --- | --- | --- |
| 1 | Short-lag linear | 5.2482 |
| 2 | 12-month seasonal naive | 1.546546 |
| 3 | Regularized linear with recent and annual lags | 1.5534 |

The tool selected trial 2 on validation and measured it once on the final holdout:
**MASE 1.3435731649**. Reloading the downloaded bundle on all 366 series / 109,280 original
observations reproduces validation and final scores to 1e-9 tolerance. The resolved contract
preserves the 12-month horizon/frequency/seasonal scaling and equal-series/origin aggregation.
Cumulative training time was 8.299 seconds. It stopped with `budget_exhausted`, without claiming
convergence or that three trials establish the best possible forecast.

The shipped panel input now uses this verified three-trial allowance; other demonstration inputs
retain eight. This is an explicit workload adjustment, not evidence that the earlier eight-trial
case now succeeds. Task, data and quality requirements are unchanged. The agent remains configurable
through caller budgets, subject to the platform execution limit.

### Final validation status

All six task modalities now have accepted autonomous models with locally reproduced scores,
across the documented runs and allowances. All six inconsistency gates were checked; both prior
receipt-fidelity failures passed targeted repeats. Current modeler coverage is 150 tests at 100%
statement/branch coverage; the real panel tool chain passes with the bundled source. No added
dependencies or coverage exemptions. The source archive is unchanged, checksum-verified, attributed,
and included in a deployment-content regression test. CI is green and the SDK 0.1.7 merge conflict
is resolved.

The engine-verification/improvement pass admitted five runs: three original retries, the bundled
eight-trial panel attempt, and the three-trial diagnostic. All are terminal. Account balance changed
from **$97.045187 to $95.589721**, an observed decrease of **$1.455466**; final available balance
equals total, and no funds were added. These are account-wide deltas, not itemized charges.

Stop reason: **representative functionality accepted for the documented example inputs**.
The successful cases do not establish repeated reliability, global optimality, or arbitrary-data
performance. Larger panel searches remain bounded by the observed runtime limit. Prior artifact
storage and usage-authorization failures did not recur in this pass.
