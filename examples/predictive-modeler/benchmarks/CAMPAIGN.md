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
It was rejected. A refined minimum-capacity probe then completed eight verified runs and reduced
oversized trials from four to one while tying all four paired CV objectives. Its exact tested
single sentence is adopted on top of the retained baseline. This is a narrow sizing benefit,
with no predictive gain or trial reduction in that paired experiment. The frozen selected design
then delivered **six of six independently verified models on new dataset families**, with improved
CV objectives over each run's simple baseline. Final-test results are reported separately below.

Final confirmed wallet balance: **$71.889028**. The campaign wallet decrease is **$23.700693 of
the $50 allowance**, leaving $26.299307. Account deltas can include other activity and are not
itemized per-run charges. No top-ups occurred. All admitted training runs are terminal and collected.

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
and the initial failed refinement preparations. The first candidate added one sentence asking for few shallow trees
when byte caps bind. Glass and Emotions each ran twice per design with unchanged three-trial,
120-second allowances, model, resources and evaluator. The preregistered decision required
preserved acceptance, no paired CV regression, and repeat-consistent quality or sizing benefit.

All four guided forests remained oversized: Glass used 50 trees with depths 6 and 4, producing
250493 and 145661 bytes against a 100000-byte cap; Emotions used ten depth-3 trees, producing
115765 and 116085 bytes against a 50000-byte cap. CV objectives tied in three pairs and regressed
in one. The phrase reduced some capacities but did not establish a useful size scale, so it was
not adopted. An attempt with no forest is reported separately, never counted as successful sizing.

The refined isolated hypothesis replaced that vague sentence with:

> If you explore a forest under a model_bytes cap, first measure one depth-one tree to establish serialized overhead, then choose capacity from that evidence.

It kept the same cases, repeats, resources and decision criteria. The
[refined probe records](forest-probe-results.json) preserve all eight verified model outcomes.
All four paired best-feasible CV objectives tied, and acceptance remained eight of eight.
Oversized trials fell from four to one. Emotions used a compliant single depth-one tree in both
guided repeats: 22261 and 19605 bytes, versus baseline forests of 66645 and 161509 bytes under
the 50000-byte cap. This repeat-consistent sizing improvement meets the preregistered criterion.

Glass illustrates the limit: guided repeat 0 used 100 depth-one trees and fit within its
100000-byte cap at 90189 bytes; repeat 1 ignored the probe instruction and used 100 depth-five
trees, producing 429325 bytes. Both baseline Glass forests exceeded the cap. A compliant forest
therefore does not establish that the requested minimum probe was followed.

There was no predictive-quality improvement, trial reduction, or speed gain: aggregate guided
training time was 69.94 seconds versus 53.03 seconds for baseline. Every forest probe was the
third and final trial, so subsequent capacity growth from measured overhead was not tested. The adopted
change is exactly the tested minimum-probe sentence, with no new tool or parameter; the rejected
vague sentence is absent. Prior failures and comparisons remain separate. These two validation
datasets support a narrow improvement in sizing decisions, not general optimality or reliable
instruction compliance on every run.

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
New submissions stopped; the other four entries in that failed batch were never attempted.
The user subsequently instructed work to continue while they debugged the backend. A fresh
eight-run refinement completed with no 502 errors and no retries. The controller now records
timestamped read errors and allows bounded retries for read-only requests and proven
pre-admission preparation failures; ambiguous admission is never resubmitted. Known admitted
runs are observed and recovered using their existing IDs. The earlier failed preparations and
balance observations remain preserved separately from the successful resumed comparison.

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

## Historical evidence and stopping decision

Historical protocols remain separate from v4:

- [Eight-run v2 pilot](cloud-pilot-results.json): all bundles verified. The complexity appendix
  tied both Penguins comparisons, lost both MPG size comparisons, and saved no trials. It was
  rejected; its one-off preparer and appendix were removed. Immutable records remain.
- Sixteen v3 runs finished before the CV change: 13 verified, one honest no-feasible result,
  and two forecast seed-contract mismatches. No v3 scores are pooled with v4.
- Earlier diagnostic cohorts retain their original failed replay/receipt audits locally.
  Reassessments never overwrite failures. Numerical canaries are diagnostic evidence, not extra
  favorable repeats in the design comparison.

The main baseline plus the exact tested minimum-probe sentence was selected and committed before
final evaluation. We stop further prompt iteration for **practical diminishing returns**, not
budget exhaustion or proof of a global optimum. The complexity appendix, adaptive-search
replacement, and vague forest guidance failed their measured goals; only the precise probe earned
its added instruction. Further speculative wording would increasingly tune to the same inspected
dataset families. Larger searches, more model families, and calibrated sizing policies remain
possible future experiments, but would add capability or evaluation scope beyond this small example.
The remaining budget is a ceiling, not an obligation to spend.

## Frozen final dataset evaluation

[Final evaluation records](final-holdout-results.json) preserve the six requests, frozen source,
run IDs, complete trial evidence, independent audits, and artifacts' hashes. The application hash
`ac746e041d36ff19eef19b20c2eb252aa717db04037f2fbf509186a0d5235b64` exactly matches the tested
refined variant. The [final dataset manifest](holdout-cases.json) was frozen before any fits, with
SHA-256 `de0b7e9684e8441126d2f66b2452fbac52f1bd8cf2fc5d6dcc691243819ef4ff`.
No final outcome changed the prompt, tools, allowances, or selection criteria.

All six runs completed, matched their authoritative receipts, and passed independent replay and
constraint checks. Each used three trials, 120 cumulative training seconds maximum, Luna, one CPU,
and 2 GiB. There were six CLI submissions, no retries, and no gateway failures. The preceding
resumed eight-run comparison was also free of gateway failures; this does not establish that the
backend issue is permanently fixed.

| Dataset / task | Objective | Simple baseline CV | Selected CV | Final test | Selected family |
| --- | --- | ---: | ---: | ---: | --- |
| Swiss banknotes / binary | F1 ↑ | 0.6667 | 0.9814 | 1.0000 | Linear |
| Wine / multiclass | Macro-F1 ↑ | 0.1909 | 0.9796 | 0.9710 | Linear |
| Yeast / multilabel | Micro-F1 ↑ | 0.4788 | 0.6249 | 0.6351 | Linear |
| Diamonds / regression | MAE ↓ | 5189.46 | 1211.67 | 1028.94 | Extra Trees |
| Chocolate interest / forecasting | MAE ↓ | 11.19 | 6.17 | 2.58 | Linear |
| ERCOT regions / panel forecasting | MAE ↓ | 1112.96 | 423.42 | 269.23 | Seasonal naive |

Baseline and selected CV scores use the same frozen folds. Only the selected model receives a
final-test measurement; no baseline final-test comparison is implied. Banknotes and diamonds meet
their three-input limits; wine and yeast meet their 100000-byte and 200000-byte caps. These cases
specified no predictive acceptance floors, so verified acceptance alone is not a claim that quality
is sufficient for every application. All six selected CV objectives improved over their measured
simple baselines, but the gains and final-test values remain specific to each dataset and split.

There is one run per new dataset, so this final collection does not estimate agent variability or
statistical superiority. The six families are now used evaluation data: tuning from their outcomes
would require retiring them as holdouts and freezing a new untouched collection.

Do not average incompatible objectives or exclude failed/missing attempts from denominators.
A higher score cannot compensate for violated requirements. These small datasets cover distinct
task semantics without establishing universal accuracy. [Source proposals](DATASET-CANDIDATES.md)
record provenance and conversion risks; broader evaluation should vary missingness, text,
imbalance, grouping and task phrasing while keeping related source families in one partition.
