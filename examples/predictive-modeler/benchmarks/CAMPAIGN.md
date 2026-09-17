# Agent-design comparison campaign

## Current evidence and next decision

The original six cloud examples are development fixtures. Their successful v1 runs establish
representative functionality, not repeated reliability or generalization. Complexity adds
measurement protocol `predictive-modeler/v2`. The prepared initial pilot remains frozen at v2.
Current code uses `predictive-modeler/v3`, correcting parsed-date ordering throughout forecasting
and historical MASE scales. Historical v2 records remain unchanged. Compare contenders only under
the same protocol; rerun both designs at v3 before comparing subsequent outcomes.

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

## Approved cloud pilot

- Two public CSV tasks: Penguins macro-F1 with size/input constraints; MPG minimum model bytes
  with MAE at most 5 mpg. Requirements are frozen in `cases.json` before launch.
- Two prompt designs, two repeats each: **eight runs** total. Repeats preserve the task/data
  split and test variation in agent behavior; they are not independent datasets.
- Three training trials and 60 cumulative training seconds per run; one CPU, 2 GiB, explicit `gpt-5.6-luna`
  for both designs, and the platform's 15-minute execution limit.
  Record the actual resolved model/version for each run; refuse comparisons if these differ.
- Execute serially, alternating design order between repeats. This avoids overlapping costs
  and makes failures easier to diagnose; independent runs could be parallelized in a larger batch.
- User-approved spending allowance: **$5**, no top-ups. Check balance before admission and after
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
all six modalities. These are not populated or evaluated holdout collections.

1. Freeze at least two additional real datasets per modality as development cases, varying
   imbalance, categorical/text inputs, missingness, series length/seasonality and panel structure.
   All variants of a dataset/source family remain in one benchmark partition.
2. Add quality-first, complexity-first and binding-constraint requests, task paraphrases,
   ambiguous requests, and genuine contradictions. Score incorrect refusals as well as
   missed contradictions. Paraphrases do not increase the independent dataset count.
3. Reserve separate validation datasets for scheduled checkpoints and a sealed final collection
   covering all six modalities. Dataset selection and task ground truth precede agent outcomes.
   None of these held-out collections is populated or claimed complete by the current pilot.
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
