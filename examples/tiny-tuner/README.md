# Tiny Tuner

Tiny Tuner searches feature and training configurations for a small classifier. It measures every
candidate on a fixed validation split and preserves the best model it actually evaluated.

## When to use it

Use this example to learn the smallest Recurse pattern: construct a candidate, measure it with an
independent scorer, revise from the evidence, and stop at a target or trial budget. It is useful
when several feature and training choices should respond to held-out measurements. A direct
implementation is simpler when the configuration is already known or needs only one evaluation.

## How the loop works

1. `extract_features` constructs a feature space.
2. `train_model` fits a deterministic candidate configuration.
3. `validate_model` measures held-out precision, recall, and F1 while extending the search history.
4. `save_best_model` writes the best validated candidate to `best-model.json`.

The classifier cannot reliably solve the task without the interaction feature, so the specialist
must learn from measured trials rather than repeat arbitrary configurations.

## Run it

Create `inputs.json`:

```json
{"target_f1": 0.75, "max_trials": 4}
```

Then run it once or deploy it as a reusable MCP tool:

```sh
recurse run examples/tiny-tuner --inputs inputs.json
recurse deploy examples/tiny-tuner --as mcp
```

## Result

The successful result reports the best measured validation metrics. The run also produces
`best-model.json` with the winning configuration, weights, bias, precision, recall, and F1.

## Limitations

The dataset and training procedure are intentionally small and deterministic. The example
demonstrates a verified tuning loop; it is not a general model-training system or a performance
benchmark.
