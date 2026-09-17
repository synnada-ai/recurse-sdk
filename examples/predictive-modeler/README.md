# Predictive Modeler

A Recurse agent that takes a dataset and a prediction goal, experiments with scikit-learn
pipelines, and returns its best feasible model with a report explaining the experiments.
It supports binary, multiclass, and multilabel classification, regression, and forecasting
for one or multiple time series.

## When to use it

Use it when an agent should choose models, features, and hyperparameters from measured results.
A direct implementation is simpler when the configuration is already known.
[Tiny Tuner](../tiny-tuner) offers a smaller introduction to the experiment-and-revise loop.

## Run it

With [uv](https://docs.astral.sh/uv/) and Python 3.14 installed, run from the repository root:

```sh
uv tool install recurse-sdk
recurse login
recurse run examples/predictive-modeler \
  --inputs examples/predictive-modeler/inputs/binary.json --memory-mib 2048
```

This runs in the Recurse cloud and consumes credits. Choose another input below to change the task.
The examples use real observations; source URLs and checksums are recorded in
[modeler/sources.json](modeler/sources.json).

| Task / input | Dataset |
| --- | --- |
| [Binary classification](inputs/binary.json) | [UCI Bank Marketing](https://doi.org/10.24432/C5K306) |
| [Multiclass classification](inputs/multiclass.json) | [UCI Dry Bean](https://doi.org/10.24432/C50S4B) |
| [Multilabel classification](inputs/multilabel.json) | [Google Research GoEmotions](https://github.com/google-research/google-research/tree/master/goemotions) |
| [Regression](inputs/regression.json) | [UCI Concrete Strength](https://doi.org/10.24432/C5PK67) |
| [Single-series forecasting](inputs/forecast.json) | [UCI Bike Sharing](https://doi.org/10.24432/C5W894) |
| [Panel forecasting](inputs/panel-forecast.json) | [Monash Tourism Monthly](https://doi.org/10.5281/zenodo.4656096) |

GoEmotions retains its official splits. Tourism forecasts 12 months across 366 series using a
bundled archive with [attribution](modeler/data/README.md); other sources download at runtime.

## Inputs

Describe the prediction problem, rather than choosing an estimator. All tasks use the same
[manifest contract](agent.yaml):

| Field | Meaning |
| --- | --- |
| `dataset` | Public HTTPS URL of a CSV or Parquet file, or an `example:` handle from the sample inputs |
| `task` | What to predict, target columns, available predictors, and any time, group, or forecast-horizon requirements |
| `quality` | Optional objective and hard constraints, each with a metric and task-specific parameters |
| `budget` | Optional `max_trials` (default 20) and cumulative `max_training_seconds` (default 600) |

For example, the binary request maximizes precision while requiring recall of at least 0.5:

```json
{
  "objective": {
    "metric": "precision", "direction": "maximize",
    "parameters": {"positive_label": "yes"}
  },
  "constraints": [{
    "metric": "recall", "parameters": {"positive_label": "yes"},
    "operator": ">=", "value": 0.5
  }]
}
```

This is the `quality` field, not a complete request. Classification supports precision, recall,
F1, and accuracy, with averaging options for multiclass/multilabel tasks. Regression supports
MAE, RMSE, and absolute bias; forecasting also supports MASE. Use lowercase metric names.

Complexity objectives or constraints use `model_bytes` (the complete serialized predictor) or
`input_feature_count` (required raw columns). See [compact regression](inputs/compact-regression.json)
and [feature-limited classification](inputs/feature-limited-multiclass.json). These are separate
from the search budget.

To use your own data, replace `dataset` with a direct HTTPS file URL and describe its columns in
`task`. The cloud runtime must be able to fetch it without authentication; local files are not
uploaded by this input contract. Downloads are limited to 32 MiB.

## How the loop works

1. **Review.** Compare the task and quality before loading data. Contradictions stop with
   `inconsistent_inputs`; material ambiguity returns `needs_clarification`.
2. **Resolve.** Inspect the schema and freeze targets, available features, metrics, and splits.
3. **Experiment.** Form hypotheses, train candidates, and choose follow-ups from measured results.
   Tools offer simple baselines, linear models, Extra Trees, and seasonal-naive forecasts.
4. **Verify.** Independent tools select the best feasible candidate and test it once on untouched
   data. A failed final constraint returns `no_feasible_model`, without test-driven retuning.

Tabular defaults reserve 20% for testing and use up to five CV folds: stratified for binary/multiclass,
shuffled for regression/multilabel, and group-disjoint or chronological when required. Forecasts
use up to three expanding-window validation origins and a final held-out horizon. Official splits
are preserved; small datasets can reduce fold counts. Each fold refits preprocessing and models.

Selection uses mean fold scores and actual saved-model complexity. Training and CV share the time
budget. The agent stops at its budget or explains why further experiments are unlikely to help.

## Result

The final receipt reports status, stop reason, summary, and artifact paths. Download them:

```sh
recurse artifacts <run-id> --output results
```

Accepted runs include `model-bundle.zip` (predictor, prediction code, and dependencies) and
`report.md` (model choice and experiment rationale). JSON evidence records the contract, splits,
trials, scores, and receipt. Unsuccessful runs explain the failure without supplying an accepted model.

## Kickstart prompt

Give this prompt to a coding assistant with the [Recurse skill](https://recurse.run/SKILL.md)
to build a similar example:

> Use the Recurse skill to build a standalone example called Predictive Modeler. The user supplies
> a public dataset-file URL or a documented dataset handle, a natural-language prediction goal,
> optional structured quality requirements, and a bounded search budget. Support binary,
> multiclass, and multilabel classification, regression, and single/panel time-series forecasting.
> Let users describe the task, not the estimator.
>
> Propose one flexible input/output contract. Quality must express one objective and multiple hard
> constraints using metrics appropriate to each task, including model size or required input count.
> First check whether the prose and structured requirements agree; stop with inconsistent_inputs
> for contradictions and request clarification for material ambiguity. Do not invent quality floors.
>
> Use scikit-learn and a small, useful modeling toolbox. Let the agent form hypotheses, choose
> experiments, and revise its approach from evidence. Keep independent evaluation and completion
> tools responsible for constraints, budgets, and preserving the best feasible model. Provide sane
> task-aware cross-validation defaults, fit preprocessing inside each fold, respect temporal/group
> boundaries, and reserve an untouched final test. Never retune from final-test results.
>
> Return a reloadable model, a concise Markdown report describing its configuration and rationale,
> and a history of every experiment, including failures. Find representative, manageable real
> datasets for each task type; use synthetic data only where suitable real data is unavailable.
> Include runnable requests, tests, source attribution, and a concise README.
>
> Agree on a total spending cap before cloud evaluation. Start with a baseline design, test small
> hypotheses under matched conditions, repeat variable outcomes, and evaluate the selected design
> on new dataset families. Verify saved artifacts independently. Iterate until further changes are
> no longer justified by evidence or the budget is reached. Keep tools and prompts simple, remove
> rejected experiments and session reports from the public example, and explain limitations without
> claiming exhaustive search or a globally best model. Preserve other examples in the repository.

## Limitations

This is a bounded CPU modeling example. It does not support arbitrary training code, custom metrics,
multitarget regression, irregular time series, future forecast covariates, or prediction intervals.
Semantic task interpretation remains an LLM judgment. Good validation results do not guarantee
performance on new data; the caller must choose meaningful metrics and constraints.

For local checks, run `uv run --directory examples/predictive-modeler/tests --locked pytest`.
The [evaluation utilities](benchmarks/README.md) support reproducible comparisons of your own changes.
