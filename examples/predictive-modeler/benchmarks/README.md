# Evaluating Predictive Modeler

These files belong to the evaluator, **not the deployed agent**. Ground-truth specifications
must never be copied into cloud inputs, prompts, or the application bundle. The public
request contains only `dataset`, `task`, `quality`, and `budget`.

## What is implemented

- A versioned suite with stable case IDs, dataset-family grouping, and explicit
  development/validation/holdout partitions. A family cannot cross partitions.
- A bounded local fixed-search baseline using the actual training/evaluation tools.
- Source, verifier, dependency, request, specification, budget, policy, and actual
  dataset fingerprints. The dataset fingerprint matches the tool's saved Parquet bytes.
- Artifact hashes, recorded trial counts, training/wall time, and preserved failures.
  Monetary cost is unknown (`null`); local computation is not claimed to be free.
- Paired comparisons that retain failures and missing cases and flag incompatible
  requests, verifiers, environments, or data. Metrics remain per case: F1 and MAE are
  never averaged together.

The fixed-search baseline receives evaluator-owned ground truth and uses three declared
configurations. It does **not** test natural-language interpretation, autonomous search,
convergence, or generalization. It uses all declared features; a feature ceiling below
that count can make every fixed candidate infeasible. Repetitions reuse the deterministic
split and estimator seeds, so they do not estimate dataset-sampling uncertainty.

## Cases and source provenance

`cases.json` currently contains only **development** cases: the existing six task modalities
with a three-trial allowance, plus two additional public CSV cases. `validation-cases.json`
contains six further comparison tasks; `holdout-cases.json` is the frozen final collection,
with source/schema/split preflight only and no fitted models yet. Related source families
and all task variants stay in the same benchmark partition.

The new CSVs are pinned to commit `71e2436a092d714350de0fc409ca8a8714e7e78f` of the
[Seaborn example-data repository](https://github.com/mwaskom/seaborn-data), with exact SHA-256
checksums in the manifest. Seaborn documents that these are convenience copies which may
be modified from canonical sources. This benchmark uses the pinned copies without edits:

- **Penguins:** 344 observations, three species, six eligible raw predictors; maximize
  macro F1 subject to model size at most 5,000,000 bytes and at most six raw input columns.
  The source repository links to the [Palmer penguins project](https://github.com/allisonhorst/penguins).
- **MPG:** 398 vehicle observations, seven eligible raw predictors; minimize model bytes
  subject to MAE at most 5 mpg. The source repository links to
  [Cars data](https://data.world/dataman-udit/cars-data).

These constraints are illustrative development requirements declared before the pilot,
not claims about production acceptability. They must not be adjusted in response to a
final holdout score. Downloads are checked against their pin and the actual loaded
observations are compared with the pinned CSV before training.

## Local commands

From `examples/predictive-modeler`, use a new output directory for every attempt:

```sh
uv run --locked python -m benchmarks run penguins /tmp/modeler-penguins-1 --design SOURCE_REVISION
uv run --locked python -m benchmarks run mpg /tmp/modeler-mpg-1 --design SOURCE_REVISION
```

`--design` is a human-readable label; recorded file hashes identify the actual source,
including uncommitted changes. `--repeat` labels repeated attempts. `--manifest` selects
a frozen suite. The fixed policy supports one to three trials and honors training limits.
Never relabel these outcomes as autonomous-agent results.

Each output directory contains `benchmark-result.json` and the tool artifacts. A failed
attempt also writes a result with a diagnostic. Collect result objects into two JSON arrays,
then compare them:

```sh
uv run --locked python -m benchmarks compare baseline-results.json contender-results.json
```

`paired` means both attempts exist. `comparable` additionally requires matching evaluation
conditions and two successful outcomes. Failed/missing attempts stay in the output and in
any campaign success-rate denominator; compare their failure rates separately from scores.
Do not compare quality only on successful runs and silently discard failures.

## Recorded local pilot

[pilot-results.json](pilot-results.json) preserves the two actual fixed-search attempts, their
content fingerprints, outcomes, artifact hashes, and independent score audits. Both accepted
models reproduce under the recorded protocol. The records are historical evidence with exact
verifier hashes; documentation/preparation changes after a run do not rewrite those hashes.
No autonomous design comparison or cloud spending is represented by these records.

## Autonomous campaign

Run cloud agents through the documented Recurse CLI, supplying **only** the case's
`request` object. Freeze source revision, case suite, dependency lock, measurement protocol,
run/training budgets, repetitions, total spending cap, and timeout policy first. Store the
full service outcome, charged cost, elapsed time, and downloaded artifacts for every run,
including failures without artifacts. The local runner does not launch or pay for cloud runs.

Use the independent trusted-artifact assessment for contract interpretation, split equality,
feasibility, selection, and reproduced scores. Loading a model bundle executes pickle code;
only assess artifacts from your own trusted runs. File hashing alone establishes artifact
integrity, not scientific validity or absence of leakage.

Independent CV refits can differ slightly across numerical libraries even with identical
source bytes, splits, preprocessing statistics and seeds. For refitted MAE, RMSE, MASE and
absolute bias only, score reproduction permits relative tolerance `1e-6` and absolute
tolerance `1e-8`. Classification scores and predictions from the saved deployment artifact
retain `1e-9` relative/absolute tolerance; model bytes and raw input counts must match exactly.
Every fold and the aggregate must reproduce, and the recorded aggregate must equal its own
fold mean under the stricter tolerance. Recomputed CV and final-test constraints must pass
the original hard bounds without any tolerance allowance. Near-equal objective scores
within numerical tolerance are not evidence that one agent design is better.

A verifier revision changes its fingerprint. Preserve original audit results and record
uniform reassessments separately; never silently overwrite failed historical audits.

The next comparison should pair the current agent and one explicitly hypothesized contender
under equal allowances. Measure correct interpretation/rejection, completion, feasible-model
rate, per-case quality/complexity, latency, and cost; report each modality separately. Only
then expand across unseen datasets and task variants. A final frozen holdout must be used
once for the selected design; tuning against it turns it into development data.
