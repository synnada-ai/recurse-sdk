# Benchmarking Predictive Modeler

There are two ways to evaluate this example:

- **Local reference:** run three fixed model configurations without an LLM or Recurse credits.
  This checks modeling tools and provides reference scores; prompt changes have no effect.
- **Cloud agent:** give Recurse a benchmark request, let the agent choose experiments, then
  independently check the downloaded model and scores. This consumes Recurse credits.

The evaluator and its expected specifications are excluded from the deployed application.
Only a case's `request` object is sent to the agent.

## 1. Set up

Install [uv](https://docs.astral.sh/uv/) and Python 3.14. From the repository root, run these
commands in the same shell:

```sh
cd examples/predictive-modeler
uv sync --locked
export BENCH_DIR="$(mktemp -d)"
export BENCH_CASE=penguins
printf 'Results directory: %s\n' "$BENCH_DIR"
```

All following commands run from this directory. Keep `$BENCH_DIR` for later inspection; choose a
fresh directory for a new experiment. Downloading real datasets requires internet access.

[cases.json](cases.json) contains these case IDs:

| ID | Task / dataset |
| --- | --- |
| `penguins` | Multiclass / Palmer penguins; small starting case |
| `mpg` | Regression / automobile fuel economy; model-size objective |
| `binary` | Binary / Bank Marketing |
| `multiclass` | Multiclass / Dry Bean |
| `multilabel` | Multilabel text / GoEmotions |
| `regression` | Regression / Concrete Strength |
| `forecast` | Single-series forecast / Bike Sharing |
| `panel-forecast` | Panel forecast / Tourism Monthly |

Set `BENCH_CASE` to another ID to use it below. Larger cases take longer. Each case declares its
own metrics, constraints, and training budget. All bundled cases are development data.

## 2. Run a local reference

```sh
uv run --locked python -m benchmarks run "$BENCH_CASE" \
  "$BENCH_DIR/baseline/$BENCH_CASE" --design baseline --repeat 0
```

This fits up to three predefined candidates, writes artifacts and `benchmark-result.json`, and
prints the result. The output directory must not already exist. `--design` labels the attempt;
actual source and dependency hashes are recorded separately. `--manifest path/to/cases.json`
selects another suite.

Check `status`, `diagnostic`, and `assessment.verified` in the result. The command can exit normally
while recording a failed experiment. A legitimate `no_feasible_model` is not a passing result.
`evaluation.validation` contains CV scores; `evaluation.final_test` contains the selected model's
untouched-test scores. Local references receive expected task specifications, so their success
does not establish that the agent understood the task.

### Compare local modeling changes

After making a modeling change, rerun the same request into a new directory. Running unchanged
code is also useful as a reproducibility check, but is not a new agent design:

```sh
uv run --locked python -m benchmarks run "$BENCH_CASE" \
  "$BENCH_DIR/contender/$BENCH_CASE" --design contender --repeat 0

uv run --locked python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["BENCH_DIR"])
for design in ("baseline", "contender"):
    paths = sorted((root / design).glob("*/benchmark-result.json"))
    if not paths:
        raise SystemExit(f"No results found for {design}")
    records = [json.loads(path.read_text()) for path in paths]
    (root / f"{design}.json").write_text(json.dumps(records, indent=2) + "\n")
PY

uv run --locked python -m benchmarks compare \
  "$BENCH_DIR/baseline.json" "$BENCH_DIR/contender.json" \
  > "$BENCH_DIR/comparison.json"
cat "$BENCH_DIR/comparison.json"
```

The comparison pairs case IDs and repeat numbers. `comparable: true` requires successful,
verified results with matching data, requests, budgets, verifier, and environment. Otherwise,
inspect `mismatch` and both original results. Changes to scoring or split code change the verifier;
reassess both sides consistently before drawing conclusions. Failures and missing cases remain
visible. To add repeats, use another output directory and increment `--repeat` on both sides;
this label does not change random seeds.

## 3. Run the actual Recurse agent

First extract the public request and keep a local copy of the evaluator case:

```sh
uv run --locked python - <<'PY'
import json
import os
from pathlib import Path

from benchmarks.runner import load_cases

case = next(c for c in load_cases(Path("benchmarks/cases.json"))
            if c["id"] == os.environ["BENCH_CASE"])
output = Path(os.environ["BENCH_DIR"]) / "cloud"
output.mkdir(exist_ok=False)
(output / "case.json").write_text(json.dumps(case, indent=2) + "\n")
(output / "request.json").write_text(json.dumps(case["request"], indent=2) + "\n")
PY

uv run --locked recurse login
uv run --locked recurse run . \
  --inputs "$BENCH_DIR/cloud/request.json" --cpu 1 --memory-mib 2048 \
  > "$BENCH_DIR/cloud/run.yaml"
```

The run requires a funded Recurse account. The request's training/trial limits are not a monetary
spending cap. Decide your allowance before starting a series of runs. Record the source revision,
model, run ID, resource settings, and service output for each attempt. For matched comparisons,
set the same explicit `agent.model` in each design's manifest.

The command writes the run ID early and appends the terminal snapshot to `run.yaml`. Exit `0` confirms
observation, so check YAML `status: succeeded` before using the result. Read the
`run_id` field from that YAML document, copy it into this variable, then inspect and download the
same run. `recurse status` emits the same YAML schema for its current snapshot:

```sh
export RUN_ID='paste-the-run-id-here'
uv run --locked recurse status "$RUN_ID"
uv run --locked recurse artifacts "$RUN_ID" --output "$BENCH_DIR/cloud/artifacts"
```

Do not resubmit merely because observation or download failed: the original run may still be
executing. Retain failures and retry inspection of its existing ID. Artifact retrieval atomically
replaces existing artifact files after verification. Retrieve artifacts within 24 hours. If no
receipt is available, retain the service error as a failed attempt rather than continuing the model
audit.

## 4. Independently assess the cloud artifacts

Run this against artifacts from **your own trusted run**; joblib/pickle loading can execute code.
Use the same frozen source and locked dependencies as the evaluated agent. The code supports all
bundled cases, verifies pinned CSV sources when present, and refits CV models locally:

```sh
uv run --locked python - <<'PY'
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
from benchmarks.assessment import assess
from benchmarks.runner import audit
from modeler.datasets import _download, load_dataset

root = Path(os.environ["BENCH_DIR"]) / "cloud"
case = json.loads((root / "case.json").read_text())
artifacts = root / "artifacts"
receipt = json.loads((artifacts / "receipt.json").read_text())
hashes = audit(artifacts, receipt)
if receipt["status"] != "succeeded":
    assessment = {"verified": False, "issues": ["No accepted model; inspect receipt.json."]}
else:
    with TemporaryDirectory() as cache:
        if "source_sha256" in case:
            raw = _download(case["request"]["dataset"])
            if hashlib.sha256(raw).hexdigest() != case["source_sha256"]:
                raise ValueError("Pinned dataset checksum mismatch")
            data = pd.read_csv(io.BytesIO(raw))
        else:
            data = load_dataset(case["request"]["dataset"], Path(cache))
        assessment = assess(case, artifacts, data)
result = {"run_id": os.environ["RUN_ID"], "case_id": case["id"],
          "receipt_status": receipt["status"], "artifact_hashes": hashes,
          "assessment": assessment}
(root / "assessment.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
raise SystemExit(0 if assessment["verified"] else 1)
PY
```

A verified result reproduces the selected model's scores and checks expected task semantics,
splits, recorded selection, and constraints. Also check that `receipt.json` agrees with the
service's final output. Hashes establish file integrity; they do not by themselves verify quality.
Numerical tolerances allow small cross-platform refit differences without relaxing hard constraints.

The `compare` command accepts local `benchmark-result.json` records, **not** this cloud assessment
summary. For cloud design comparisons, repeat steps 3–4 with fresh directories and matched requests,
models, budgets, and verifier versions; compare verified CV objectives per case alongside completion,
constraint satisfaction, trial counts, time, and cost. Do not average incompatible metric units or
silently discard failures. Refitting for assessment uses local compute outside the cloud-run budget.

Freeze a design before evaluating new dataset families. Use final tests only for reporting and
acceptance; never tune against their outcomes. Changing the verifier requires consistent reassessment
of both designs, with original failures preserved. Keep generated results outside the source tree.
