# Agent-design comparison campaign

## Current evidence and next decision

The original six cloud examples are development fixtures. Their successful v1 runs establish
representative functionality, not repeated reliability or generalization. Complexity adds
measurement protocol `predictive-modeler/v2`. The prepared initial pilot remains frozen at v2.
Current code uses `predictive-modeler/v4`: task-appropriate cross-validation with fresh fold fits,
a separate final test, and deployment-artifact complexity. It retains v3 parsed-date ordering.
Historical v2/v3 evidence remains unchanged and is not pooled with v4 comparisons.

The new Penguins and MPG cases are also **development** data once inspected here. The fixed-search
pilot checks the evaluator and provides a cheap reference, not a test of an autonomous agent.
Both keep their full real source data, with a pinned CSV URL/checksum. No synthetic benchmark
replaces a supported modality. Small generated unit-test fixtures only exercise edge cases.

## Pilot hypothesis and controlled change

- **Baseline:** current committed agent, including complexity support. Both prepared apps explicitly pin `gpt-5.6-luna`
  to avoid a service-default change between attempts.
- **Contender:** identical tools, dependencies and input/output schemas; append
  `complexity-aware.md` to its prompt. The only change asks the agent to diagnose predictive
  versus complexity failures and choose experiments that can plausibly improve feasibility
  or the objective under the remaining allowance.
- **Hypothesis:** this reasoning avoids wasted experiments and produces at least comparable
  feasible outcomes with fewer trials, less time, or improved objective values. It may have
  no benefit when constraints are loose; report that rather than declaring a win.
- **Local reference:** the fixed baseline/linear/extra-trees probes. It receives the expected
  specification and cannot be compared on natural-language interpretation. Its trial and
  training budgets match the autonomous request, but its lack of LLM overhead is explicit.

Prepare isolated applications and an exact schedule locally:

```sh
uv run --locked python -m benchmarks.prepare /tmp/modeler-pilot
```

This creates two applications, two public request files, and a private `plan.json` with expected
specifications and content hashes. It performs no cloud calls. Both prepared applications are
packaging-tested: evaluator files are absent. Do not copy `plan.json` into either application.

## Completed initial cloud pilot

- Two public CSV tasks: Penguins macro-F1 with size/input constraints; MPG minimum model bytes
  with MAE at most 5 mpg. Requirements are frozen in `cases.json` before launch.
- Two prompt designs, two repeats each: **eight runs** total. Repeats preserve the task/data
  split and test variation in agent behavior; they are not independent datasets.
- Three training trials and 60 cumulative training seconds per run; one CPU, 2 GiB, explicit `gpt-5.6-luna`
  for both designs, and the platform's 15-minute execution limit.
  Record the actual resolved model/version for each run; refuse comparisons if these differ.
- Execute serially, alternating design order between repeats. This avoids overlapping costs
  and makes failures easier to diagnose; independent runs could be parallelized in a larger batch.
- Initial user-approved spending allowance: **$5**, subsequently expanded to **$50 total**
  for the design campaign, including this pilot, with no top-ups. Check balance before admission and after
  each terminal run; stop starting work before the remaining allowance cannot reasonably cover
  another run. Balance deltas can include other account activity and are not itemized invoices.
- Stop early on repeated infrastructure failure, retrieve all available evidence, and do not
  automatically relaunch unknown-state runs. A timeout is a failure, not convergence.

For each scheduled entry, run the corresponding app and request through `recurse run` with
`--cpu 1 --memory-mib 2048`; retain its run ID, public terminal status, receipt, actual model
identity, elapsed time, and available cost evidence. Download artifacts promptly. Match the
public output exactly to `receipt.json`. Independently assess trusted accepted bundles using
`assessment.assess(case, downloaded_directory, original_dataframe)` under the same dependencies.
The local CLI does not automatically orchestrate this paid batch.

## Initial pilot outcome and current comparison

All eight v2 autonomous runs completed, their public outputs exactly matched the receipts,
and independent model/artifact audits passed. All four paired comparisons were comparable.
The [immutable records](cloud-pilot-results.json) retain run IDs, hashes, measurements and costs.

| Task | Repeat | Baseline objective | Appendix objective |
| --- | --- | ---: | ---: |
| Penguins: maximize validation macro F1 | 1 | 1.0 | 1.0 |
| Penguins | 2 | 1.0 | 1.0 |
| MPG: minimize model bytes | 1 | 3927 | 5111 |
| MPG | 2 | 3638 | 3927 |

All MPG final MAEs passed the unchanged limit of 5. Every run used three trials. The appendix
lost both size comparisons and saved no trials, so it is not selected. Observed wallet decrease
was $1.629812. These two development datasets do not establish general convergence.

The subsequent frozen v3 comparison replaced mandatory family diversity with objective-directed
experiments and permits stopping when a feasible validation objective reaches its mathematical
bound. Duplicate configuration lists are removed. Both designs share the parsed-chronology fix
and clarified baseline/tie tool descriptions. There are no new runtime tools or dependencies.

The batch planned 32 attempts: two designs, two repeats, and eight real datasets. It repeats
Penguins and MPG and adds diagnostic breast-cancer classification, multiclass glass,
house-price regression, music-emotion multilabel classification, airline-passenger forecasting,
and a five-series stock-price panel. Four independent cloud jobs run at once, each using
`gpt-5.6-luna`, one CPU, 2 GiB and three trials. New cases allow 120 training seconds; the two
pilot tasks retain 60 seconds. Within every paired comparison the resources are identical.
The total campaign allowance is $50, including the initial pilot, with no top-ups.

Six additional source families are reserved for final evaluation after selecting a frozen design.
The [validation requests](validation-cases.json) are frozen before their cloud outcomes.
The final collection preflight checks only source bytes, schemas, contract validity and split validity; no
models have been fitted. Keep those outcomes separate from development and validation choices.

## Decision rules

Report every scheduled attempt, including missing artifacts and infrastructure failures.
A correct task interpretation, frozen split, all required constraints, reproducible selected
model, and valid completion receipt are prerequisites for an accepted result. The artifact
audit checks recorded selection and reproduces the selected model, but cannot prove an LLM
never saw holdout data or reconstruct discarded candidate fits.

Keep quality comparisons within matching case/repeat, request, data, verifier, environment,
service model, and resource allowances. Summarize acceptance rate first, then objective values,
required inputs, bytes, trials, wall time and cost. Keep per-task-family results visible;
never average raw F1 and MAE, or exclude failed runs from success-rate denominators.

This tiny pilot may justify continuing a hypothesis, fixing instrumentation, or rejecting a
specific prompt change. It is insufficient to select a generally superior agent design.
Do not change thresholds after seeing the final-test results. Any verifier change requires
re-running both baseline and contender; retain older outcomes separately.

## Expansion before a generalization claim

[Dataset candidates](DATASET-CANDIDATES.md) records sourced proposals and conversion risks for
all six modalities. These source proposals informed the frozen validation requests and reserved final collection.

1. Freeze at least two additional real datasets per modality as development cases, varying
   imbalance, categorical/text inputs, missingness, series length/seasonality and panel structure.
   All variants of a dataset/source family remain in one benchmark partition.
2. Add quality-first, complexity-first and binding-constraint requests, task paraphrases,
   ambiguous requests, and genuine contradictions. Score incorrect refusals as well as
   missed contradictions. Paraphrases do not increase the independent dataset count.
3. Reserve separate validation datasets for scheduled checkpoints and a sealed final collection
   covering all six modalities. Dataset selection and task ground truth precede agent outcomes.
   The final collection remains unfitted during design comparison; metadata preflight is not
   performance evaluation.
4. Compare specific hypotheses (profiling, experiment allocation, constraint diagnosis) with
   equal resources and repeated paired runs. Inspect failures and resolve contradictory evidence
   before changing designs. A repaired example alone is not generalization evidence.
5. Select a frozen design from development/validation evidence, then assess it once on the
   final collection. Keep within-dataset train/validation/test separation distinct from this
   across-dataset agent-design separation. Further tuning consumes that holdout and requires
   a new untouched collection.
6. Stop on agreed acceptance criteria, evidenced diminishing returns, a resource limit, or
   a concrete blocker. Report uncertainty and weak task families instead of claiming global
   optimality. Review runtime complexity, resource cleanup and idiomatic implementation before
   adopting any change.

## Cross-validation milestone

The v3 comparison stopped admitting work when default CV was requested. All 16 launched runs
completed and their artifacts were collected: 13 passed independent verification, one honestly
reported no feasible model, and two forecast runs selected seed 0 instead of the frozen seed 42.
The second repeat was never launched. Observed total wallet decrease including the initial pilot
was $5.368644; $44.631356 of the campaign allowance remains. No backend execution failures
without diagnostics occurred in these 16 runs.

V4 reserves a final test and refits every validation fold. Fold fitting/scoring consumes the same
training budget as deployment fitting. Official partitions remain intact. Independent assessment
replays CV and verifies the saved deployment model. Future prompt comparisons must restart with
both designs on this protocol and explicitly preserve the requested seed. The six reserved final
dataset families remain untrained; no generalization or design-convergence claim is made yet.
