# Backpack 3D

Astra writes a complete Python modeling program from a backpack photograph. Recurse executes the
program, exports its actual geometry as GLB, renders three views, and gives those images back to
Astra for visual critique and revision. The selected model remains editable Python code.

## When to use it

Use this example to explore whether extra test-time compute improves a visual construction task.
It preserves a strong first attempt and lets the specialist change its geometry strategy, inspect
mistakes, and keep or reject each revision. A good first model is the baseline to beat; extra
attempts are useful only when the resulting models visibly improve.

The application explicitly selects `gpt-6-astra`. Use a Recurse service that supports this model;
it does not fall back to another model. No reference photograph or finished answer model is bundled.

## How the loop works

1. `reference_image` returns the photograph as an image data URI. The runtime's image adapter
   delivers this as native visual input to the specialist.
2. `build_candidate` executes a complete Python program defining `scene: trimesh.Scene` and an
   optional `camera` dictionary. The program can invent meshes, surfaces, sweeps, textures, and
   shared geometric constraints; there is no fixed primitive catalog.
3. The tool reloads the exported GLB and renders the matched view, side, and back. It returns a
   labeled comparison image directly, avoiding an extra model call just to view the result.
4. `record_review` stores the visible defects and whether this candidate replaces the incumbent.
   `revise_candidate` applies exact edits to a saved program; `read_candidate` allows a full rewrite.
5. `finish_run` saves the selected model, source, receipt, and complete attempt history.

Execution failures count against `max_trials` and remain in the history. Successful execution
proves the program produced renderable mesh geometry. Selection is Astra's subjective visual
judgment, not a numeric accuracy or independent quality guarantee. Side and back views expose
construction problems but cannot establish the true appearance of unseen surfaces.

## Run it

From the SDK repository root, use a photograph you can share with the Recurse service. This creates
an input file from `backpack.jpg` without requiring an imaging library:

```sh
python - <<'PY'
import base64
import json
from pathlib import Path

encoded = base64.b64encode(Path("backpack.jpg").read_bytes()).decode("ascii")
if len(encoded) > 90_000:
    raise SystemExit("Use a smaller JPEG (at most 67.5 KB) for this example.")
inputs = {
    "reference_image_base64": encoded,
    "description": "A burgundy fabric backpack with a mustard front pocket, top handle, and straps.",
    "max_trials": 4,
}
Path("inputs.json").write_text(json.dumps(inputs))
PY
export RECURSE_API_URL=https://api.dev.recurse.run
uv run recurse login
uv run recurse run examples/backpack-3d --inputs inputs.json --cpu 2 --memory-mib 2048
uv run recurse artifacts <run-id> --output backpack-results
```

The commands target the DEV service used for the Astra experiment. Set `RECURSE_API_URL` to
your Astra-enabled service URL if different, before both login and the run.

The reference may be PNG or JPEG. Prefer compressed JPEG for photographs: current runtime inputs
travel through an environment variable, so large base64 images can prevent process startup. This
example caps the encoded image at 90,000 characters to leave headroom for the other inputs. Keep
the description short. Native image input requires the service's image adapter; an SDK install
alone does not add this server capability.

Start with four evaluations. For larger searches, preserve the output of short runs and pass the
selected `best.py` plus its critique in the next run's optional `task` input. Ask the specialist to
reproduce that incumbent first, then improve it. Reproductions and failed attempts both count as
evaluations. Short runs also reduce exposure to service time limits; the initial DEV experiment hit
a callback authorization expiry after roughly nine minutes in a long run. The public CLI currently has no
per-run timeout flag.

Do not require `task` in the input schema: the runtime consumes that field as the task instruction
before validating tool inputs. The default task references the description and trial budget.

## Result

The final response contains `candidate_id`, `attempts`, and `valid_candidates`. Downloaded artifacts
include:

- `best.glb`: the selected model in standard glTF Y-up coordinates.
- `best.py`: its complete editable modeling program (Z up, front along negative Y).
- `best.png` and `best-comparison.jpg`: the matched preview and reference/three-view comparison.
- `selection.json` and `receipt.json`: the selected critique and actual evaluation counts.
- `history.zip`: every attempt's source, parent, hypothesis, result, execution log, and available
  model, images, and review. Failed attempts are preserved too.
- `checkpoint-001`, `checkpoint-004`, `checkpoint-016`, `checkpoint-032`: the incumbent when a
  candidate is reviewed at that local run count. These are snapshots inside one search, not
  independent measurements of how different compute budgets perform.

For manual review, compare the first and selected models against the photograph, inspect the
pocket, handle, seams, silhouette, and strap attachments, then orbit the exported GLB in an
independent viewer. Read rejected candidates and critiques to check whether selection was useful.
Do not compare only the best-looking screenshot or treat triangle count as model quality.

## Limitations

A single photograph leaves hidden geometry ambiguous. The preview is a small diffuse software
renderer, not a full PBR renderer: it supports solid colors and approximates textures by vertex
sampling. It converts glTF linear material factors and sRGB texture colors for display, but
lighting, roughness, transparency, and fine texture details can differ in another viewer.

Generated programs execute arbitrary Python inside the hosted Recurse sandbox. The subprocess's
60-second timeout limits modeling execution, not rendering or the whole specialist run. Running
the tools locally executes generated code with your local account's permissions. This is an
experimental visual modeling workflow, not a sandbox implementation or a production asset checker.

## Development

The tests execute real modeling programs and reimport actual GLBs. They cover native image
payloads, custom geometry, coordinate/material preservation, normal repair dependencies, failed
builds, exact revisions, incumbent preservation, archive contents, and raster color/occlusion.
They do not prove a model sees an image or that a generated backpack looks good; those require a
live specialist run and visual review.

```sh
uv run --directory examples/backpack-3d/tests --locked \
  pytest --cov --cov-branch --cov-report=term-missing -q
MYPYPATH=../../../src uv run --directory examples/backpack-3d/tests --locked \
  mypy --config-file pyproject.toml ../tools.py test_backpack.py
./check.sh
```

The example's dependencies and tests use a separate locked environment, following RNA Fold Lab.
The repository quality gate runs these checks and requires 100% statement and branch coverage.
