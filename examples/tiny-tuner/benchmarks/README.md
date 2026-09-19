# Cloud refinement evidence

These are autonomous agent runs, distinct from the earlier hand-selected local smoke tests.
A service status of `succeeded` means the agent returned a valid receipt; feasibility requires
`target_reached: true`. The smallest qualifying model found so far has **4,018 parameters at 99.036670% mean CV**.
The consolidated final policy reproduced its exact scores and checkpoint. The earlier 5,698-parameter recipe reproduced the same fold scores in
two independent Astra searches and two consolidated Luna policy runs. This is fixed-seed repeatability, not an independent generalization estimate or proof
of a globally smallest network. Final policy repeats and compact hypotheses are still running.

## Fixed comparison protocol

- Target: arithmetic mean CV accuracy >= 0.99.
- Full 60,000-example MNIST training set; official test split unused.
- Stratified three-fold CV, one repetition; split seed 7, training seed 7.
- Search allowance: 300 seconds from first profile or evaluation, 30 attempted recipes, 10 epochs/fold max.
- Cloud ceiling: 4 CPU, 4096 MiB; baseline tools use one intra-op training thread; later revisions may use the full ceiling.
- Agent models: gpt-5.6-luna and gpt-6-astra, recorded per run in `runs.json`.
  Runtime SDK 0.1.8, PyTorch 2.14.0 CPU. Separately labeled experiments permit 30 epochs.

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

## Budget-aware hypothesis

Uniform widening can consume roughly four times the convolution work when adjacent channel
counts both double. A prompt revision asks the agent to estimate full-CV cost using completed
trial timing, retain a time margin, and consider optimization or depth before uniform widening.
This revision changes the search policy only; evaluator, architecture tools and budget remain
identical. The following section records its measured outcome.

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
At this stage neither feasibility nor diminishing returns had been established.

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
The checkpoint is preserved; the subsequent sections record repeat and size-reduction results.

## Compact refinement and repeat evidence

The 17,850-parameter recipe reproduced the same fold scores in runs
`f1aa4c3b-2afb-46b7-abb5-cd5a031144a8`, `71b940a3-85f3-466a-a1de-8d175c777d14`,
and `b98cd508-b4dd-4cf4-ba4f-cd6a66bfccc3`. Their smaller follow-ups timed out, so these
runs establish repeatability but do not establish a size floor. A spatial-shrink attempt
`bda1efdf-f1d2-42ec-bbb0-bae27f4925e6` failed in infrastructure with no retained measurements;
the successful isolated retry is the third run above.

Astra run `cf54b303-5639-48b8-9e9e-f2f377b5668b` found CNN(8,16,32), head 3,
Adam 0.003/cosine, batch norm/ReLU/max, weight decay 0.0001, batch 128, ten epochs:
8,890 parameters and 0.9899666573 mean CV. It remained below the exact cutoff.

The follow-up `e036cb3f-73da-4243-aa9d-571e0720adb6` raised initial LR to 0.004:

| Channels | Parameters | Mean CV | Qualifies |
| --- | --- | --- | --- |
| (8,16,32) | 8,890 | 0.9903833406 | Yes |
| (8,16,24) | 6,994 | 0.9897167148 | No |
| (8,16,28) | **7,942** | **0.9903833781** | **Yes** |

The final recipe has one convolution per stage and head 3; all other settings above are
unchanged. It completed by 262.12 seconds, with fold scores 0.9897520496/0.9912495625/0.9901485223.
This is the smallest qualifying model measured at this point, not a global minimum.
The identical-agent repeat reproduced this result. A separately labeled
experiment permits 30 epochs under the same 300-second wall limit; those results must be
identified as an expanded training search space rather than silently mixed with the 10-epoch cap.

## Smaller-first search and cosine endpoint

`92d4c52c-a0aa-44fc-be0f-7d0c920633dc` (Astra) found **5,698 parameters at 0.9904832523**:
CNN(8,16), two convolutions per stage, head 3, batch norm/ReLU/max, Adam 0.003/cosine,
weight decay 0.0001, batch 128 and ten epochs. Fold scores were 0.9910017996,0.9910995550,
0.9893484023. It completed by 140.88 seconds. A 4,018-parameter (8,12) follow-up trained for
seven epochs scored 0.9879167197. It is not a full 10-epoch result for that smaller recipe.
The Luna smaller-first run attempted (12,24), profiled 252.72 seconds of training, and timed
out before full CV. Narrowing channels did not guarantee cheaper CPU execution.

The 7,942-parameter agent repeated its exact scores in `2ea0b3b7-ca75-436f-a72b-39b22f1d2d8b`.
A lower cosine endpoint (min_lr_ratio 0.01) in `1df6b578-cee6-4c57-9960-e35236d5211a`
raised the otherwise unchanged 6,994-parameter CNN(8,16,24) from 0.9897167148 to 0.9901499898.
Its smaller 6,046-parameter (8,16,20) trial scored 0.9898833573. Scores below 0.99 were not
rounded into qualification. The 5,698-parameter result remains the smallest measured so far;
its exact repeat is recorded below.


## Compact neighborhood and final policy

The 5,698-parameter recipe reproduced exactly in `a30cfe20-8c9c-415c-a485-6f732bcda22b`.
Nearby completed measurements retained the 10-epoch default cap:

| Revision | Parameters | Mean CV | Meets 99% |
| --- | --- | --- | --- |
| Two-convolution (8,12), head 3, cosine endpoint 0.03 | 4,018 | 0.9893833439 | No |
| Two-convolution (8,14), head 3, original optimizer | 4,822 | 0.9897166298 | No |
| Two-convolution (8,16), head 2, LR 0.004 | 4,898 | 0.9891665973 | No |
| Consolidated policy, Luna | 5,698 | 0.9904832523 | Yes |
| Consolidated policy, Astra | 5,698 | 0.9896665014 | No |

The head-2 result came from a run allowing up to 30 epochs, but that completed trial used
only ten; its subsequent 20-epoch trial timed out. The other expanded-cap experiment completed
a 6,994-parameter 15-epoch recipe at 0.9902166465. These results do not show that longer training
is universally unhelpful. A (6,16) attempt timed out with no completed score and provides no
size-floor evidence.

The consolidated prompt describes the measured starting region, requests cost profiling,
and leaves architecture and training choices adaptive. Under identical inputs, Luna run
`a48078e1-1715-404f-85dc-b22181a0a53d` reproduced the 5,698-parameter recipe and scores by
136.84 seconds. Astra run `e9f42737-1482-4b51-b44a-9421c6cead37` profiled the same starting
recipe at 304.78 estimated training seconds and instead chose eight epochs, batch 256,
and LR 0.004. That completed below target. Host variability and short profiling samples limit
this comparison; it does not establish a general ranking of the language models.


The consolidated Luna repeat `8d46348e-7e98-4f0a-8a0a-41a72aa89c85` reproduced the same
5,698-parameter checkpoint and scores by 148.44 seconds. An exploratory hybrid with a regular
first convolution and depthwise-separable subsequent blocks scored 0.9855834288 at 2,434
parameters in `fc1c1cf8-3076-4cb7-9042-74a0433d0050`. It needed 272.46 seconds, leaving little
headroom for widening. This measured tradeoff did not justify adding the block to the shipped
agent; it does not disprove hybrid architectures under other budgets.

## Further gain from optimization

Run `2a281950-498b-40b0-b827-585b6425641f` revisited the 4,822-parameter CNN(8,14), two
convolutions per stage, head 3. Raising Adam's initial learning rate from 0.003 to 0.004 and
lowering the cosine endpoint from 0.1 to 0.05 produced **0.9901166456 mean CV** by 186.22
seconds. Other settings stayed batch norm/ReLU/max, weight decay 0.0001, batch 128, ten epochs.
Fold accuracies were 0.9901019796, 0.9907995400, 0.9894484173. This is a joint optimization
change; the experiment does not isolate the individual contribution of learning rate versus
endpoint. The checkpoint passed strict reload, parameter recount and finite-logit inference.
A repeat and narrower-width investigations are running; the new size improvement means the
previous neighborhood was not yet at diminishing returns.


The 4,822-parameter recipe reproduced exactly in `594fb842-1ada-467e-99b8-e8f1041277b1`
by 182.26 seconds. Applying its optimizer to narrower second stages gave:

| Channels | Parameters | Mean CV | Completed by | Meets 99% |
| --- | --- | --- | --- | --- |
| (8,12) | 4,018 | 0.9897167289 | 189.59 s | No |
| (8,13) | **4,411** | **0.9901166073** | 195.14 s | Yes |

Runs are `d2c88c8b-4774-4ee6-afad-e5bc799b3ead` and
`47705808-70b3-4c1e-9952-b7c208daf8d8`. The 4,411-parameter fold scores are
0.9906018796, 0.9901995100 and 0.9895484323. Its checkpoint strictly reloads with the
canonical implementation and matches the claimed trainable parameter count. Consolidated
policy validation, stronger (8,12) optimization and asymmetric (7,13) channels are being tested.


## Optimizer refinement continues

The 4,411-parameter consolidated policy `8b2de630-e77c-409c-9faa-f0a9d1b5d540` reproduced
the original scores and checkpoint. Further optimizer refinement in
`186261f1-8f0a-45c9-9784-4531abc66fa1` made CNN(8,12) qualify at **4,018 parameters and
0.9903667006 mean CV** by 182.71 seconds. Initial Adam LR 0.005 and cosine endpoint 0.01
replaced 0.004/0.05; all other settings stayed unchanged. Fold accuracies were 0.9898520296,
0.9911495575 and 0.9900985148. Policy validation and width-11/10 variants are in progress.

The asymmetric (7,13) hypothesis encountered an initial preparation failure and then run
`58641032-f802-4733-a525-507c48e2ba5a` ended `infrastructure_failed` without artifacts.
This supplies no accuracy or convergence evidence; no claim that asymmetric channels fail
is supported by this run.


## Local capacity boundary

The consolidated 4,018-parameter policy `8281f604-066c-4d77-b237-7be49f229538` reproduced
its exact scores and checkpoint. With the same optimizer, width reductions completed below target:

| Channels | Parameters | Mean CV | Meets 99% |
| --- | --- | --- | --- |
| (8,12) | 4,018 | 0.9903667006 | Yes |
| (8,11) | 3,643 | 0.9894000473 | No |
| (8,10) | 3,286 | 0.9880000072 | No |

The smaller runs are `261ffd1e-9472-48e6-b9c9-9c73e231bd40` and
`6e8c3759-f167-4eec-9e89-3c81855b6171`. This trend motivates checking whether another
optimizer change or reallocating channels to (7,12) can recover accuracy before concluding
that gains have flattened in this local neighborhood. Timed-out follow-ups are not negative
accuracy evidence.
