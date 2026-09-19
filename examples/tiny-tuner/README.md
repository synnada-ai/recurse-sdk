# Tiny Tuner

Tiny Tuner searches for the **smallest neural network by trainable parameter count** that
reaches a target mean cross-validation accuracy on MNIST. Defaults are **0.99 accuracy**,
**300 seconds**, and three stratified folds over all 60,000 official training examples.
It keeps shrinking after it reaches the accuracy target and preserves the smallest qualifying
recipe it actually measured. It reports the smallest **found**, not a proven global minimum.

The cloud refinement campaign found a **4,018-parameter CNN at 99.0367% mean CV** under
these defaults. The recipe reproduced across two independent cloud runs with fixed seeds.
See [cloud experiments](benchmarks/README.md) for the measured comparisons and limitations.

## When to use it

Use this example when architecture and training decisions should respond to measured results.
The agent owns experiment selection: propose a network, measure its CV accuracy and size, form
a hypothesis, and spend remaining time testing cheaper or smaller alternatives.
A direct implementation is simpler when the architecture is fixed or an exhaustive sweep is
small enough. This example replaces the previous synthetic-data/F1 tuning contract.

## Blocks the agent can consider

| Family / option | Implemented blocks |
| --- | --- |
| MLP | Flatten 28×28 pixels, one to three dense hidden layers, ten-class linear head |
| CNN | One to three stages of one or two padded 3×3 convolutions, normalization/activation after each, 2×2 pooling after each stage; configurable adaptive average pooling (side 1–7, default 2) and a linear head |
| Separable CNN | Each convolution replaced by a depthwise 3×3 convolution and a pointwise 1×1 convolution; same pooling and head |
| Patch attention | Non-overlapping 7×7 patch projection (16 tokens), learned positions, one attention head, residual attention and feedforward layers with 2× expansion, mean token pooling and a linear head |
| Normalization | None; batch normalization for MLP/CNN; layer normalization for MLP/attention; one-group group normalization for CNN |
| Activation / pooling | ReLU or GELU; max or average 2×2 pooling in CNNs |
| Training | Adam learning rate, L2 weight decay, minibatch size, epochs per fold, and constant/cosine learning-rate schedule |

Widths range from 1 to 256; attention uses one embedding width. Biases, normalization affine
parameters, and learned positions count toward size. Batch-normalization running statistics
are buffers and do not count. No pruning, quantization, augmentation, pretrained weights,
convolutional residual blocks, or arbitrary Python architectures are included. Attention is an
optional experiment; the agent need not spend its limited budget testing every family.
Cosine scheduling decreases the initial learning rate to a configurable final fraction
(default 10%) across epochs, resetting for each fold. A one-epoch recipe uses its initial learning rate.

## How the loop works

- `design_network` constructs a bounded recipe without training.
- `profile_network` times a small discarded training sample and estimates full-CV training
  cost. It starts/consumes the shared allowance, produces no accuracy score, and saves timing
  evidence in `profiles.json`. Estimates exclude full validation and tool latency; reserve margin.
- `evaluate_network` trains a fresh CPU model for each configured CV split. It measures
  held-out accuracy, records its arithmetic mean and population standard deviation, counts
  parameters, and saves a checkpoint. The official test split is never used for selection.
- `finish_search` independently selects from durable trial records and returns the final receipt.
  Feasible trials rank by parameter count, then higher CV accuracy, then earlier trial. If none
  qualifies, the highest-accuracy completed trial is saved as a diagnostic with
  `target_reached: false`. If none completed, metrics are null and there is no model artifact.

The 300-second wall-clock allowance starts at the first profile or evaluation and includes data loading,
training, scoring, and time between calls. Checks occur between minibatches and after data
loading: an in-flight download or tensor operation can overrun the deadline. This is a
cooperative search deadline, not a hard process timeout or a guarantee on total agent latency.
There is also a default cap of 30 attempted recipes and 10 epochs per fold. Failed and timed-out
attempts consume trials; partial CV results cannot qualify. Repeating a recipe returns its cached
status without training again. Evaluation calls serialize and use four intra-op CPU threads and channels-last image storage.
The harness tool timeout is 660 seconds, above the maximum configurable 600-second search
allowance, so its default 30-second timeout cannot truncate a CV trial.
Completion does no additional training. Earlier completion requires the agent to justify
diminishing returns; merely reaching the target is not a stopping condition.

## Limitations

Splits and fold initialization seeds remain fixed across candidates. Pixel scaling is fixed at
1/255, and normalization statistics are fit inside each training fold. Smaller `samples` values
select a subset balanced as far as class availability permits and are useful for smoke tests; these are **subset experiments**, not
full-MNIST results. The full dataset retains its original class proportions. Reusing CV for
adaptive selection can overfit the selection metric; the score is not an unbiased estimate of
final generalization. CPU reproducibility is scoped to the same software/platform.

## Run it

Run the default search with the cloud benchmark resource ceiling:

```sh
recurse run examples/tiny-tuner --cpu 4 --memory-mib 4096
```

Or create `inputs.json` with overrides:

```json
{"target_accuracy": 0.99, "max_seconds": 300, "max_trials": 30, "cv_folds": 3, "samples": 60000, "seed": 7}
```

```sh
recurse run examples/tiny-tuner --inputs inputs.json --cpu 4 --memory-mib 4096
recurse deploy examples/tiny-tuner --as mcp --cpu 4 --memory-mib 4096
```

The first profile or evaluation downloads MNIST using torchvision's checked dataset cache in the temporary
directory. Network access is required for an uncached run. PyTorch and torchvision are isolated
example dependencies; Linux uses CPU wheels. Five minutes is a search allowance, not a promise
that any particular architecture will reach 99% on a given machine.

## Configuring cross-validation

| Input | Default | Meaning |
| --- | --- | --- |
| `cv_method` | `stratified_kfold` | `stratified_kfold`, shuffled ordinary `kfold`, or `stratified_holdout` |
| `cv_folds` | `3` | Folds per repetition for either K-fold method |
| `cv_repeats` | `1` | Repetitions using successive split seeds, keeping the same selected examples |
| `validation_fraction` | `0.2` | Per-class validation fraction for holdout; ignored for K-fold |
| `cv_seed` | `7` | Sample selection and split seed; independent of model training seed |
| `seed` | `7` | Model initialization and minibatch seed |
| `samples` | `60000` | Fixed cohort of examples used by the protocol |

Each K-fold repetition holds out every selected observation exactly once. Holdout repetitions
may overlap validation observations; they do not cover each observation exactly once. The
metric remains the arithmetic mean of all split accuracies. Batch-normalization statistics
are always fit on training observations only. Protocol version 3 records these settings in
model metadata; its default partitions reproduce the original baseline. Version 3 caps
subset class quotas at available counts and rejects a trial if its final scoring operation
finishes after the shared deadline. Earlier completed scores inside the deadline remain
comparable on the full dataset. Compare agent designs
only with identical resolved evaluation settings, and keep experiments under alternative CV
methods separate from the primary ranking. No method uses the official MNIST test split.

## Result and artifacts

The final JSON receipt reports `target_reached`, `cv_accuracy`, `parameter_count`,
`trials_attempted`, and `stop_reason`. Artifacts are:

- `trials.json`: every attempted recipe, status, and completed measurement.
- `trial-N.pt`: CPU state dictionary from the last fold of each completed trial.
- `best-model.pt` and `best-model.json`: selected checkpoint plus recipe, fold scores,
  checkpoint fold index, training example count, and protocol.
- `receipt.json`: authoritative final output; repeated completion returns the same receipt.
- `search.json`: resolved protocol (even when no trial completes) and the shared monotonic
  deadline, whose timestamp is meaningful only on the execution host.

The saved checkpoint is trained on **the last fold's training partition**, not all 60,000
examples. Mean CV accuracy describes the training recipe across folds, not those particular
weights. Keeping a fold checkpoint avoids an unbudgeted final refit. Reconstruct the network
with the bundled `_network(Candidate(...))` helper and load the state dictionary using
`torch.load(..., weights_only=True)`; convert JSON `widths` to a tuple first. The recipe and
weights are both required, including batch-normalization buffers where applicable. Call
`model.eval()` before inference; use channels-last model/image storage to reproduce the
training runtime layout.

## Local verification

```sh
uv run --directory examples/tiny-tuner/tests --locked pytest -n auto --maxprocesses=2 --cov
```

Offline tests train on synthetic image fixtures and cover block construction, reproducibility,
fold isolation, ranking, budgets, failures, and checkpoint reloads. Synthetic fixture results
are not MNIST accuracy evidence. Real-MNIST smoke tests require the dataset download and are
kept separate from offline CI.
