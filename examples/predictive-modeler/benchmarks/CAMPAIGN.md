# Agent-design comparison campaign

## Current result

The completed main comparison retains the **baseline prompt**. All 32 model slots delivered
independently verified results. Across the 12 validation pairs, baseline wins six, the
adaptive-search variant wins one, and five tie. No repeat-consistent adaptive advantage appeared;
baseline performed better in both repeats on Houses and AirPassengers. Development pairs are
reported separately. These results support retaining the baseline, not universal superiority.

[Main comparison records](cloud-validation-v4-results.json) preserve requests/data/source/verifier
fingerprints, run IDs, trial configurations, scores and audit provenance. There were 34 submissions
for 32 model slots: each design delivered 16 verified models from 17 submissions, including one
pre-admission preparation failure each. A later artifact download failed and was recovered from
the **same run**, with checksum verification and separate preserved failure/recovery records.

A focused eight-run comparison then tested forest-size guidance. All eight models verified, but
the added sentence failed to prevent oversized forests and did not improve selected-model quality.
It was rejected. A more precise minimum-capacity probe is prepared but **blocked by service
failures before model execution**. No experimental sentence has been added to the deployed prompt.
The six final dataset families remain untrained; overall design convergence is not claimed.

Last confirmed campaign wallet decrease: **$20.169835 of the $50 allowance**, leaving $29.830165
before the latest failed preparation attempts. Their follow-up balance reads also failed, so the
final balance is unavailable. Account deltas can include other activity and are not itemized
per-run charges. No top-ups occurred. All admitted training runs are terminal and collected.

## Controlled main comparison

- **Baseline:** establish a simple baseline, compare substantively different approaches before
  local tuning, then choose experiments from measured evidence.
- **Contender:** replace mandatory family diversity with choices driven by objective/constraint
  gaps, and permit stopping at a feasible mathematical optimum. Tools, schemas and dependencies
  are identical. This change remains outside the deployed prompt.
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

The outcome is explained by actual trial choices, not just win counts. Houses baseline runs
explored different four-input representations, while the contender reused the same subset.
AirPassengers differences came from lag choices and, in one contender run, never evaluating an
annual linear model. MPG pruning helped the contender once, but baseline also pruned successfully
in the second repeat. Hypotheses sometimes misdescribed configurations in both designs; measured
configurations are authoritative. Family-diversity wording is not proven inherently superior.
Every main-comparison run used three trials; the contender did not save trials.

## Forest-capacity refinement

[Forest experiment records](forest-capacity-results.json) preserve both the completed comparison
and the blocked refinement. The first candidate added one sentence asking for few shallow trees
when byte caps bind. Glass and Emotions each ran twice per design with unchanged three-trial,
120-second allowances, model, resources and evaluator. The preregistered decision required
preserved acceptance, no paired CV regression, and repeat-consistent quality or sizing benefit.

All four guided forests remained oversized: Glass used 50 trees with depths 6 and 4, producing
250493 and 145661 bytes against a 100000-byte cap; Emotions used ten depth-3 trees, producing
115765 and 116085 bytes against a 50000-byte cap. CV objectives tied in three pairs and regressed
in one. The phrase reduced some capacities but did not establish a useful size scale, so it was
not adopted. An attempt with no forest is reported separately, never counted as successful sizing.

The next isolated hypothesis replaces that vague sentence with:

> If you explore a forest under a model_bytes cap, first measure one depth-one tree to establish serialized overhead, then choose capacity from that evidence.

It keeps the same cases, repeats, resources and decision criteria. It changes one conditional
search instruction, with no new tool or parameter. **There are no model outcomes for this
refinement**, so the sizing hypothesis is unresolved rather than disproved. Prior results are
retained, and neither candidate sentence is part of the current deployed prompt.

## Service failures and recovery

The user-authorized retry completed all five missing main-comparison models. One AirPassengers
artifact download failed temporarily; all ten artifacts were subsequently retrieved from the same
run and matched the recorded checksums/sizes. The current evaluator then verified its receipt,
split plan and scores. This recovery did not launch a replacement training run.

A read-only balance guard initially failed before the first forest batch submitted anything.
Retrying that check succeeded, and all eight subsequent models completed. The refined batch then
failed on all four first-repeat submissions with this full CLI output:

```text
Packaging application...
Uploading source distribution...
Preparing runtime...
error: request_failed: Bad Gateway (HTTP 502). The Recurse service could not complete the request.
```

No run ID or admission recovery reference was returned. Installed SDK 0.1.7 calls run admission
only after preparation returns and prints an admission reference if that later request fails.
These failures occurred during preparation, before predictive tools executed. All four balance
observations also returned Bad Gateway. No server stack trace or underlying cause was available.
New submissions stopped; the other four refinement entries were never attempted. Every already
admitted training run is collected. Existing spending authorization remains; service recovery is
needed before continuing the refined experiment and final evaluation.

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

The main comparison retains 14 unaffected classification attempts from revision `2255d36`,
13 corrected-runtime attempts from `52355f8`, and five resumed attempts from `a28f747`. All 27
retained bundles were uniformly re-audited under the current verifier before comparison with the
five resumed runs. Original audits and source hashes remain explicit. **Old continuous outcomes
from before the Ridge correction are excluded from ranking.** All forest attempts use `a28f747`.

## Historical evidence and next decision

Historical protocols remain separate from v4:

- [Eight-run v2 pilot](cloud-pilot-results.json): all bundles verified. The complexity appendix
  tied both Penguins comparisons, lost both MPG size comparisons, and saved no trials. It was
  rejected; its one-off preparer and appendix were removed. Immutable records remain.
- Sixteen v3 runs finished before the CV change: 13 verified, one honest no-feasible result,
  and two forecast seed-contract mismatches. No v3 scores are pooled with v4.
- Earlier diagnostic cohorts retain their original failed replay/receipt audits locally.
  Reassessments never overwrite failures. Numerical canaries are diagnostic evidence, not extra
  favorable repeats in the design comparison.

Resume the refined forest test after service recovery, preserving its failed submissions. Select
a frozen design from development/validation evidence, then evaluate the
[frozen final collection](holdout-cases.json) once: Swiss banknotes, wine, multilabel yeast,
diamonds, chocolate search interest, and ERCOT regional demand. Only source/schema/split preflight
has run; **no holdout models have been fitted**. Its SHA-256 remains
`de0b7e9684e8441126d2f66b2452fbac52f1bd8cf2fc5d6dcc691243819ef4ff`.
Tuning from final outcomes would consume that holdout and require a new untouched collection.

Do not average incompatible objectives or exclude failed/missing attempts from denominators.
A higher score cannot compensate for violated requirements. These small datasets cover distinct
task semantics without establishing universal accuracy. [Source proposals](DATASET-CANDIDATES.md)
record provenance and conversion risks; broader evaluation should vary missingness, text,
imbalance, grouping and task phrasing while keeping related source families in one partition.
