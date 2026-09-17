# Agent-design comparison campaign

## Current status

Default task-aware cross-validation is implemented. The canonical prompt remains the baseline;
**design comparison is paused and incomplete**, with no convergence or general-superiority claim.
All admitted runs have finished and their artifacts are collected. Two later submissions failed
with HTTP 502 during application preparation, including one at reduced concurrency.

[Versioned v4 records](cloud-validation-v4-results.json) retain 27 independently verified model
results, two failed preparation submissions, and five missing slots from the 32-slot comparison.
The selected cohorts contain 29 observed submissions: 13/14 baseline submissions and 14/15
contender submissions delivered verified models. The two failures occurred before run admission;
these denominators must not be mistaken for complete 16-run-per-design success estimates.

The whole campaign's observed wallet decrease is **$17.175701 of the $50 allowance**; $32.824299
remains. This includes earlier diagnostic attempts and canaries. Wallet changes can include other
account activity and are not itemized per-run charges. No funds were added.

## Controlled comparison

- **Baseline:** establish a simple baseline, compare substantively different approaches before
  local tuning, then choose experiments from measured evidence.
- **Contender:** replace mandatory family diversity with choices driven by objective/constraint
  gaps, and permit stopping at a feasible mathematical optimum. Tools, schemas and dependencies
  are identical. No contender instructions are added to the deployed prompt without evidence.
- **Cases:** Penguins and MPG development tasks, plus six frozen
  [validation requests](validation-cases.json): WDBC binary classification, multiclass Glass,
  Houses regression, Emotions multilabel classification, AirPassengers, and a five-series Stocks
  forecast. Two repeats per case/design measure agent variability, not independent datasets.
- **Allowances:** explicit `gpt-5.6-luna`, one CPU, 2 GiB, three trials each. Penguins/MPG allow 60
  cumulative training seconds; other cases allow 120. Deployment fitting and CV fitting/scoring
  both consume this allowance. The platform has a separate 15-minute execution limit.
- **Controls:** identical public requests, seed 42, data checksums, expected specifications,
  resource ceilings and verifier within pairs. Alternate design order between repeats; independent
  jobs may run concurrently. Evaluator answers are excluded from application bundles.

Of nine completed validation pairs, baseline wins four, contender wins one, and four tie. Three
validation pairs remain incomplete. Development pairs are reported separately; neither raw F1
and MAE nor development and validation outcomes are averaged into an overall quality score.
These partial results do not select a winner. All observed runs used their three-trial allowance.

## Evaluation and numerical verification

Protocol `predictive-modeler/v4` uses fresh preprocessing/model fits in every validation fold,
a separate final test, and actual deployment-artifact complexity. See the
[evaluation policy](../README.md#evaluation-and-model-selection). Predictive constraints apply to
mean fold scores. Final-test scores cannot guide subsequent model selection or refitting.

An initial cross-platform replay exposed poorly converged default Ridge LSQR fits on correlated
predictors. The runtime now uses `tol=1e-12, max_iter=10000`, tested against an independent SVD
ridge reference. Cloud canaries actually exercised Ridge on MPG and AirPassengers: all five MPG
folds and all three forecast origins reproduced within 7e-11 absolute MAE. Saved predictor scores
also reproduced. The verifier's bounded refit tolerance was not widened to hide underconvergence.

The current comparison retains 14 classification attempts from revision `2255d36`, uniformly
re-audited with the corrected verifier. Their classifier code paths are unaffected by the Ridge
change. Thirteen additional model runs use corrected revision `52355f8`. Original source/verifier
hashes and reassessment fingerprints remain explicit. **All old continuous-model outcomes are
excluded from ranking.** Later missing-temporal-date rejection and obsolete-preparer cleanup
have local test coverage; the frozen cloud runs predate those changes.

The two preparation logs contain only `Packaging application...` followed by:

```text
error: request_failed: Bad Gateway (HTTP 502). The Recurse service could not complete the request.
```

Installed SDK 0.1.7 calls run admission only after preparation returns. Neither failed submission
reached source upload, runtime preparation completion, or run admission. Preserve the original
errors and their later diagnosis separately; these are not unexplained `execution_failed` model
runs. New admissions stopped after the repeated error, and every admitted run was collected.

## Historical evidence

Historical protocols remain separate from v4:

- [Eight-run v2 pilot](cloud-pilot-results.json): all bundles verified. The added complexity
  appendix tied both Penguins comparisons, lost both MPG size comparisons, and saved no trials.
  It was rejected; its one-off preparer and appendix were removed. Immutable results remain.
- Sixteen v3 runs finished before the CV change: 13 verified, one honest no-feasible result,
  and two forecast seed-contract mismatches. No v3 scores are pooled with v4.
- Early v4 diagnostic cohorts and their original failed replay/receipt audits are retained locally.
  Reassessments never overwrite original failures. Numerical canaries are diagnostic evidence,
  not extra favorable repeats in the design comparison.

## Next decision and final evaluation

After preparation works, complete the five missing model slots with explicit new submission
records. Reassess both designs uniformly if the verifier changes. Compare reliable delivery,
then within-case validation objectives, trials, time and measured cost. A failed or missing
attempt stays in the denominator; a higher score cannot compensate for violated requirements.

One trace-based follow-up hypothesis is recorded but **not launched**: start forests with lower
capacity when a model-size cap binds. Nine of ten inspected classification forest trials exceeded
their byte cap. The sole feasible reduced forest underperformed linear, so better sizing has not
yet demonstrated better selected-model quality. Test a minimal instruction with matched repeats
before keeping it; do not accumulate untested prompt appendices or tools.

The [frozen final collection](holdout-cases.json) contains Swiss banknotes, wine, multilabel yeast,
diamonds, chocolate search interest, and ERCOT regional demand. Only source/schema/split preflight
has run; **no holdout models have been fitted**. Its SHA-256 is
`de0b7e9684e8441126d2f66b2452fbac52f1bd8cf2fc5d6dcc691243819ef4ff`.
Select a frozen design from development/validation evidence, then evaluate this collection once.
Tuning from final outcomes would consume that holdout and require a new untouched collection.

These small real datasets cover different task semantics without establishing universal accuracy.
[Source proposals](DATASET-CANDIDATES.md) record their provenance and conversion risks. Future
broader evaluation should vary missingness, text/categorical inputs, imbalance, grouping, task
paraphrases and ambiguity while keeping related source families within one partition.
