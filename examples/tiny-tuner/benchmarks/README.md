# Cloud refinement evidence

These are autonomous agent runs, distinct from the earlier hand-selected local smoke tests.
A service status of `succeeded` means the agent returned a valid receipt; feasibility requires
`target_reached: true`. No run below establishes 99% accuracy or a globally smallest network.

## Fixed comparison protocol

- Target: arithmetic mean CV accuracy >= 0.99.
- Full 60,000-example MNIST training set; official test split unused.
- Stratified three-fold CV, one repetition; split seed 7, training seed 7.
- Search allowance: 300 seconds from first evaluation, 30 attempted recipes, 10 epochs/fold max.
- Cloud ceiling: 4 CPU, 4096 MiB; tools currently use one intra-op training thread.
- Agent model: service-resolved gpt-5.6-luna. Runtime SDK 0.1.8, PyTorch 2.14.0 CPU.

## Baseline — 2026-09-18

Run `63d6b4ae-9706-471e-809f-5ce88ba605d6` completed and its seven artifacts were retrieved.
The best completed recipe was CNN(16,32), batch normalization, ReLU, max pooling, Adam learning
rate 0.001, weight decay 0.0001, batch size 128, and five epochs per fold.

- Parameters: 6,186.
- Fold accuracies: 0.9679564087182564, 0.9697484874243713, 0.9707456118417762.
- Mean: 0.969483502661468; population standard deviation: 0.0011540006382122364.
- Completed after 107.923 seconds; **target not reached**.
- Second recipe doubled widths to (32,64) with the same training settings. It exhausted the
  remaining allowance and returned `timed_out`, with no qualifying partial CV score.
- Receipt: `stop_reason: wall_clock`, `trials_attempted: 2`, `target_reached: false`.

## Next hypothesis

Uniform widening can consume roughly four times the convolution work when adjacent channel
counts both double. A prompt revision asks the agent to estimate full-CV cost using completed
trial timing, retain a time margin, and consider optimization or depth before uniform widening.
This revision changes the search policy only; evaluator, architecture tools and budget remain
identical. It is an experiment, not yet an established improvement.

## Build diagnostics

Earlier cloud preparations failed before run admission. Standard-library and SDK-only controls
succeeded, and a separate execution-runtime probe successfully installed and imported the exact
PyTorch stack. After the backend repair and SDK 0.1.8 upgrade, the full baseline built and ran.
The index's R2 mirror independently returned HTTP 403 while canonical wheel URLs returned 200;
that observation alone did not establish the preparation failure's root cause.

## Budget-aware prompt — 2026-09-18

Run `78cae77c-5c9e-4892-a0b5-ee80aad3bd07` returned a valid receipt but no completed model.
Its first recipe was CNN(16,32,64), batch normalization, ReLU, max pooling, learning rate 0.001,
weight decay 0.0001, batch size 128, and eight epochs per fold. It consumed the entire
300-second allowance and returned `timed_out`. Final accuracy and parameter count are null.
All four diagnostic artifacts were retrieved. The baseline remains the better measured design.

The revision emphasized depth and optimization alternatives but lacked a measured cost anchor
for the first trial. It chose a deeper, longer first experiment that could not finish. This
observation does not disprove cost-aware planning; it shows this prompt alone did not supply
or enforce a reliable estimate. Future revisions need measurable trial-cost feedback or
training-throughput improvements before claiming the five-minute 99% objective is attainable.
No evidence of diminishing returns or a successful 99% agent has been established yet.
