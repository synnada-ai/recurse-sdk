# Predictive Modeler

Give the agent a dataset and a prediction goal. It checks that your prose and structured quality
requirements agree, experiments with scikit-learn pipelines, and returns the best feasible
pipeline it evaluated, with a report and reproducible evidence.

## When to use it

Use this example when feature choices, model families, or decision thresholds should respond to
measured experiments. The agent owns the hypotheses and experiment selection; independent tools
own the evaluation rules, budget, winner selection, and artifacts. A direct implementation is
simpler when the configuration is already known or only one evaluation is needed.

## Input contract

Four fields serve every supported prediction task:

| Field | Required | Meaning |
| --- | --- | --- |
| `dataset` | Yes | HTTPS CSV/Parquet URI or one of the real-data `example:` handles below |
| `task` | Yes | What to predict, target columns, available features, prediction time, and grouping/horizon requirements |
| `quality` | No | One objective and zero or more hard metric constraints |
| `budget` | No | `max_trials` (default 20, maximum 100) and `max_training_seconds` (default/maximum 600) |

For example, [inputs/binary.json](inputs/binary.json) describes predicting bank subscriptions
before a call. It maximizes precision with a recall floor; the agent must exclude call duration.
Every other input file uses the same signature. Users describe the task, not an estimator.

The agent first calls `get_request`, compares structured quality with the task in its prompt, and
calls `review_inputs` with an independent interpretation of explicit prose requirements. Semantic
contradictions return `inconsistent_inputs`; material ambiguities return `needs_clarification`.
The tools enforce that a review precedes downloading/training and compare extracted metric
requirements. Semantic understanding itself remains an LLM judgment, not a deterministic proof.
Neither input source silently overrides the other. Compatible additional options are preserved.

After inspecting the table, the agent freezes a resolved contract: task family, targets, features,
label interpretation, split policy, metric options, and dataset fingerprint. Changing these rules
requires a new run. Prose-only requirements still apply. With no supplied objective, use the
prose objective or default to F1 for classification and MAE for regression/forecasting. The agent
never invents a mandatory quality threshold.

### Metrics and constraints

Each objective has `metric`, `direction` (`maximize` or `minimize`), and optional `parameters`.
Each constraint has `metric`, optional `parameters`, `operator` (`>=` or `<=`), and `value`.
Unsupported metric names/options are rejected, rather than interpreted ad hoc.

| Task | Metrics | Parameters |
| --- | --- | --- |
| Binary classification | precision, recall, F1, accuracy | `average`, `positive_label`, optional `label` |
| Multiclass classification | precision, recall, F1, accuracy | `average`, optional per-class `label` |
| Multilabel classification | precision, recall, F1, accuracy | `average`, optional target-column `label` |
| Regression | MAE, RMSE, absolute bias | None |
| Forecasting | MAE, RMSE, absolute bias, MASE | MASE accepts `seasonal_period` |

Use lowercase names: `f1`, `mae`, `rmse`, `absolute_bias`, `mase`. Classification averaging supports
`binary`, `micro`, `macro`, `weighted`, and multilabel `samples`, subject to task compatibility.
Default averaging is binary for binary tasks and macro otherwise. Class names are normalized to
strings. Specify the positive label when its meaning matters. Accuracy has no parameters;
multilabel accuracy is exact label-set match. Zero-division precision/recall/F1 score zero.
A `label` selects one class/label; use a separate constraint for each class that needs protection.

Forecast errors are averaged uniformly across series and forecast origins. Within an origin,
steps have equal weight. MASE scales each series/origin by the historical seasonal-naive MAE,
using only history before that origin. An undefined scale makes the candidate infeasible.
This version does not expose arbitrary aggregation weights or custom metric code.

## Real datasets and runnable tasks

All six examples use real observations. Downloaded source bytes are pinned in
[modeler/sources.json](modeler/sources.json); a checksum mismatch stops loading.

| Input | Handle | Dataset and attribution | Size / purpose |
| --- | --- | --- | --- |
| [Binary](inputs/binary.json) | `example:bank-marketing` | [Moro, Rita & Cortez, Bank Marketing, UCI](https://doi.org/10.24432/C5K306) | 41,188 rows; categorical/numeric features, precision–recall trade-offs |
| [Multiclass](inputs/multiclass.json) | `example:dry-bean` | [Koklu & Ozkan, Dry Bean, UCI](https://doi.org/10.24432/C50S4B) | 13,611 rows, 16 numeric features, 7 bean varieties |
| [Multilabel](inputs/multilabel.json) | `example:goemotions` | [Demszky et al., GoEmotions, Google Research](https://github.com/google-research/google-research/tree/master/goemotions) | 54,263 filtered comments, all 28 emotion/neutral labels |
| [Regression](inputs/regression.json) | `example:concrete` | [Yeh, Concrete Compressive Strength, UCI](https://doi.org/10.24432/C5PK67) | 1,030 rows, 8 predictors, measured strength in MPa |
| [Forecast](inputs/forecast.json) | `example:bike-sharing` | [Fanaee-T, Bike Sharing, UCI](https://doi.org/10.24432/C5W894) | 731 daily observations; forecast 14 days |
| [Panel forecast](inputs/panel-forecast.json) | `example:tourism-monthly` | [Godahewa et al., Monash Tourism Monthly](https://doi.org/10.5281/zenodo.4656096); original data: Athanasopoulos et al., *The tourism forecasting competition* (2011) | 366 monthly series, 109,280 observations; forecast 12 months |

The UCI pages, [Google Research dataset license](https://github.com/google-research/google-research),
and [Monash dataset metadata](https://huggingface.co/datasets/Monash-University/monash_tsf)
identify CC BY 4.0 licensing. Preserve attribution when redistributing data or converted tables.
The application ships the source manifest and conversion code, not copies of the datasets.

Conversions preserve observed values: XLS/XLSX become tables; concrete columns receive short
names; GoEmotions labels become binary columns and retain official train/validation/test splits;
Tourism TSF becomes `series,date,value`. GoEmotions source URLs pin a Git commit. No labels or
series are selected for favorable model performance. GoEmotions contains subjective annotations
and potentially offensive text; see its authors' documented limitations.

The bank request demonstrates a stratified classification split, not future-campaign performance.
Call duration is unavailable before the call. Bike forecasts exclude contemporaneous
`casual`/`registered` counts and future observed weather. Tourism uses 12-month horizons so even
the shortest series retain enough training history after two validation origins and one final
horizon. It does not reproduce the competition's original evaluation protocol.

## How the loop works

1. `get_request` and `review_inputs` establish input consistency before downloading or training.
2. `inspect_dataset` loads data and reports schema/missingness without returning held-out rows.
3. `resolve_problem` freezes the task and disjoint split membership.
4. `train_candidate` fits the agent's configuration under a subprocess deadline.
5. `evaluate_candidate` scores that candidate independently on the frozen validation split.
6. `experiment_history` provides all attempts, hypotheses, failures, and measurements.
7. `finish_run` selects the best feasible evaluated candidate, measures it once on the final test,
   writes the report, and packages an accepted pipeline.

The agent compares hypotheses and decides which follow-up experiments are justified. A constraint
passing does not itself end optimization. Stops distinguish exhausted budget from explained
diminishing returns. No global-optimality claim or exhaustive enumeration is made.

### Modeling toolbox

- **Tabular:** training-only median/most-frequent imputation, optional numeric scaling, one-hot
  categories, and TF–IDF for selected text columns.
- **Classification:** prior baseline, logistic regression, extra trees; optional class weighting.
- **Multilabel:** independent classifiers or classifier chains; a shared prediction threshold.
- **Regression:** mean baseline, ridge regression, extra trees.
- **Forecasting:** last-value and seasonal-naive baselines, ridge or extra trees on selected lags
  and calendar features. Each series has its own model; predictions recurse through the horizon.

Candidate options and defaults are defined in `validate_configuration` and described by the
training tool. Bounds limit tree counts/depth, vocabulary sizes, and lag counts. Failed attempts
remain in history and consume budget. The same normalized configuration cannot be repeated.

Trials are serialized with a run-local transaction and one native compute thread. Their measured
subprocess durations, including interpreter startup, count toward the cumulative training budget.
The budget does not include download, LLM reasoning, evaluation, or artifact-writing time; the
platform's total run limit still applies. Larger CPU allocation will not make this version's
serial fits parallel. There is no mutable global run state.

### Evaluation and model selection

Tabular tasks use a fixed 60/20/20 train/validation/test split, or official assignments. Random
binary/multiclass splits are stratified. Group splits keep groups disjoint. Temporal splits keep
equal timestamps together and order partitions. A classification training partition must contain
every class. Repeated validation drives search, so final-test data is never used to pick a model.

Forecast validation uses two successive, non-overlapping full horizons per series. Actual targets
from the first horizon become historical observations only at the second origin. The final test
is the next horizon. Forecast estimators retain their initial training fit; only observed history
advances. Series have their own origins and no cross-series features. No future actual target is
fed into lag features during a horizon.

All hard constraints must pass for a candidate to compete on its objective. Undefined metrics
cannot pass. Ties preserve the earlier candidate. A final-test constraint failure reports
`no_feasible_model`; there is no test-driven fallback selection or restarted tuning.

## Run it

From the repository root, run one request:

```sh
recurse run examples/predictive-modeler \
  --inputs examples/predictive-modeler/inputs/binary.json --memory-mib 2048
```

Select another input file from the table to change the task. These commands use the remote
Recurse runtime and consume credits. Public examples require network access but no dataset login.
Your own data must be available to that runtime through an HTTPS CSV/Parquet URI; a laptop's local
path is not uploaded by this contract. Downloads and expanded ZIP members are limited to 32 MiB.

To make the agent reusable as MCP:

```sh
recurse deploy examples/predictive-modeler --as mcp --memory-mib 2048
```

## Result

The agent returns the exact completion receipt, with `status`, `stop_reason`, `summary`,
`questions`, `conflicts`, and workspace-relative `artifacts` paths. Supported statuses are
`succeeded`, `no_feasible_model`, `inconsistent_inputs`, `needs_clarification`, and
`unsupported_task`. Infrastructure failures remain runtime failures.

Artifacts include:

- `model-bundle.zip` on accepted results: the complete fitted pipeline, importable prediction
  code, contract, and pinned prediction dependencies.
- `report.md`: task interpretation, agent rationale, trial configurations, metrics, and limitations.
- `trials.jsonl`: every attempted configuration, hypothesis, duration, failure, and validation result.
- `resolved-contract.json` and `splits.json`: exact semantics, data fingerprint, and row membership.
- `evaluation.json`: selected trial, validation metrics, final-test measurements, and acceptance.
- `environment.json`: Python and prediction dependency versions for accepted results.
- `receipt.json`: authoritative final result; repeated finalization returns the same receipt.

No accepted model is supplied when constraints fail. Reports and experimental evidence remain.
Private intermediate data/models are removed on normal finalization; abrupt runtime termination
may leave diagnostic state. Download artifacts through `recurse artifacts <run-id> --output results`.

Extract an accepted model bundle into a new directory, use Python 3.14, and run:

```sh
uv venv
uv pip install -r requirements.txt
uv run python predict.py new-data.csv
```

Run from that extracted directory. Classification/regression inputs contain the declared feature
columns. Forecast inputs contain each series' observed history through the forecast origin,
including target, time, and series columns. The output covers the configured next horizon.
Only load trusted model bundles: joblib uses Python pickle semantics. The bundle retains the exact
selected training-only fit; it is not silently refitted on validation or test data.

## Local verification

The isolated test environment keeps scientific packages out of the SDK's dependencies:

```sh
uv run --directory examples/predictive-modeler/tests --locked \
  pytest --cov --cov-branch --cov-report=term-missing -q
```

The default suite uses small generated fixtures solely for deterministic edge cases and requires
100% statement and branch coverage. The real-data suite is opt-in and uses full datasets:

```sh
uv run --directory examples/predictive-modeler/tests --locked \
  pytest -m integration -n auto -q
```

It verifies downloads/checksums, task resolution, actual subprocess fitting, independent scoring,
final selection, and reloadable artifacts for each task. `tests/specifications.json` is the
expected resolution fixture; the runtime agent does not consume it. The integration test uses
fixed probes to verify tools, not to prescribe agent search behavior. Set `MODELER_EVIDENCE_DIR`
to an absolute directory to retain reports and bundles. Existing files there are replaced.

These local runs establish tool behavior. Autonomous task interpretation, consistency judgment,
and experimental decision-making require separate end-to-end cloud-agent validation. Do not
report local tool probes as evidence that the autonomous agent passed those checks.

## Limitations

This is a bounded CPU modeling example, not unrestricted AutoML. It supports binary/multiclass
single-target classification, binary-column multilabel tasks, single-target regression, and
regular single/panel forecasting. It does not support arbitrary training code, multilabel targets
stored as unparsed lists, multioutput regression, irregular forecasting, future covariates,
custom metrics, probability calibration, or prediction intervals. Return actionable unsupported
or clarification outcomes rather than silently changing such tasks.

Fixed validation scores are subject to selection bias; a final holdout reduces but does not
eliminate uncertainty. Metric thresholds are empirical acceptance checks, not confidence bounds.
The real datasets demonstrate different task semantics without establishing universal accuracy
or production suitability.
