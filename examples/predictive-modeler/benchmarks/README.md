# Evaluating Predictive Modeler

These reusable utilities assess model artifacts and compare candidate designs. They are excluded
from the deployed application. Expected task specifications belong to the evaluator; cloud inputs
should contain only the public `dataset`, `task`, `quality`, and `budget` fields.

## Run a local reference

From `examples/predictive-modeler`, use a fresh output directory:

```sh
uv run --locked python -m benchmarks run penguins /tmp/modeler-penguins --design baseline
```

[cases.json](cases.json) contains real-data development requests, source checksums, expected
interpretations, and budgets. `--manifest` selects another suite; `--repeat` labels an attempt.
The fixed reference tries up to three declared configurations using the actual modeling tools.
It tests training and evaluation, not the agent's natural-language interpretation or autonomous
experiment choices. A reference can legitimately fail its constraints.

Each run writes `benchmark-result.json` and model artifacts, recording dataset, request, source,
verifier, and dependency fingerprints. Failed attempts retain diagnostics. Keep generated results
outside the example's source tree.

## Compare designs

Collect result objects into two JSON arrays, then run:

```sh
uv run --locked python -m benchmarks compare baseline-results.json contender-results.json
```

Comparisons require matching cases, data, evaluation rules, and budgets. Failed or missing attempts
remain explicit. Compare task-specific scores separately; do not average F1 and MAE together.
For cloud evaluation, run each public request through `recurse run`, retrieve its artifacts, and
use the independent assessment to verify interpretation, feasibility, selection, and scores.
Only load model bundles from trusted runs: joblib/pickle loading can execute code.

Use separate dataset families for development, validation, and final evaluation. Freeze the chosen
design before the final collection; tuning from those results makes it development data. Preserve
original failures when reassessing under a changed verifier, and compare all designs consistently.
Numerical tolerances permit small cross-platform refit differences without relaxing hard constraints.
