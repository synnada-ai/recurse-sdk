# Cloud refinement evidence

These are autonomous agent runs, distinct from the earlier hand-selected local smoke tests.
A service status of `succeeded` means the agent returned a valid receipt; feasibility requires
`target_reached: true`. The deeper-block revision below first exceeds 99%; no run proves a globally smallest network.

## Fixed comparison protocol

- Target: arithmetic mean CV accuracy >= 0.99.
- Full 60,000-example MNIST training set; official test split unused.
- Stratified three-fold CV, one repetition; split seed 7, training seed 7.
- Search allowance: 300 seconds from first evaluation, 30 attempted recipes, 10 epochs/fold max.
- Cloud ceiling: 4 CPU, 4096 MiB; baseline tools use one intra-op training thread; later revisions may use the full ceiling.
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

## CPU layout timing probe

Run `d7286977-1ab7-470c-9a66-08fc9cbaa3d3` measured a synthetic CNN(16,32) batch of 128,
with batch normalization, under the same 4-CPU ceiling. Two warm-up batches preceded ten
measured batches per combination. Seconds per training batch:

| Threads | Contiguous | Channels-last |
| --- | --- | --- |
| 1 | 0.036184 | 0.016384 |
| 2 | 0.023492 | 0.015770 |
| 4 | 0.015791 | 0.013920 |

This is short-run throughput evidence, not an accuracy result or a guarantee of sustained
speed. It motivates channels-last storage and four intra-op threads. Concurrent autonomous
comparisons isolate throughput changes from a further spatial-head/prompt revision.

## Throughput and spatial-head comparisons

| Revision | Run | Best mean CV | Parameters | Completed / attempted |
| --- | --- | --- | --- | --- |
| Throughput only | `69c7d944-1875-4a85-9d85-660a91d16682` | 0.9852166222 | 26,090 | 4 / 5 |
| Spatial head + timing guidance | `cbe3b78a-47fd-4081-978b-975693d74b54` | 0.9842666296 | 10,026 | 3 / 3 |

Neither qualifies. The throughput-only winner was CNN(16,32,64), batch normalization,
ReLU/max pooling, ten epochs, Adam 0.001, weight decay 0.0001, batch 128, fixed 2x2 head.
Its five-epoch trial scored 0.9848499188; separable(16,32,64) with ten epochs scored 0.9760169095;
CNN(8,16,32) with ten epochs scored 0.9842331671. The final wider attempt timed out.
The spatial-head winner was CNN(16,32), head 4, ten epochs, batch normalization, no weight decay,
Adam 0.001, batch 128. Widening to (32,64) for five epochs scored 0.9836332030. A head 7 trial
with five epochs, Adam 0.003 and batch 256 scored 0.9814834763. It finished with 3.81 seconds
remaining and stopped without another trial. This is not evidence that overall improvements
have flattened out: optimization schedules and additional nonlinear depth remain untested.

Protocol 3 subsequently fixes near-full subset quotas and rejects scoring that finishes after
the deadline. These full-data completed records all finished inside the allowance, so their
scores remain comparable; future repeats use the tightened verifier.

## Broader hypotheses and first feasible result

Complete recipes, fold scores, profiles and receipts are retained in [runs.json](runs.json).
All comparisons retain the fixed full-data protocol and resource ceiling above.

| Revision | Best mean CV | Parameters | Meets 99% |
| --- | --- | --- | --- |
| Profile-guided prompt | 0.9769831870 | 6,186 | No |
| Separable/spatial | 0.9802832529 | 9,172 | No |
| Cosine/spatial | 0.9833334030 | 10,026 | No |
| Compact depth | 0.9897667356 | 26,090 | No |
| Attention hypothesis | 0.9711833127 | 6,186 | No |
| Two convolutions per stage | **0.9908166740** | **17,850** | **Yes** |

The profile-guided run never called the profiler. The separable run did: its 185-second
training estimate preceded a 173-second cumulative completed measurement, useful but not a
hard runtime guarantee. The attention recipe itself scored 0.9381668036 at 38,346 parameters;
the table reports that run's better CNN diagnostic.

The first qualifying run `9ffb0d0f-9f6e-45cb-aa52-d4c0797932f3` used CNN(16,32), two
convolutions per stage, batch normalization, ReLU/max pooling, head 2, ten epochs, batch 128,
Adam 0.001 with cosine decay, and weight decay 0.0001. Fold accuracies were 0.9905018996,
0.9916995850 and 0.9902485373; population standard deviation 0.0006328228. It completed
as trial 2 by 195.43 seconds. Its otherwise identical one-convolution-per-stage control scored
0.9747499828. The attempted shrink to (12,24) exhausted the remaining budget without a score.
The checkpoint is preserved, and repeat/size-reduction experiments are ongoing.
