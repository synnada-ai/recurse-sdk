# Autonomous-agent validation

The user authorized this batch on September 15, 2026. Two runs were attempted; both ended in
`infrastructure_failed` without agent output or artifacts. The remaining ten were not launched
because the failure repeated. Local tests exercise the tools with supplied interpretations and
fixed candidate probes; they do not establish LLM understanding or search quality.

## Execution evidence

Both attempts used source commit `5d19a87d7543c0a8f528cfbe9de348b596c5e34e`, the service-selected
`gpt-5.6-luna` model, one CPU, and 2 GiB memory. Runtime preparation completed before admission.

| Input | Run ID | Outcome |
| --- | --- | --- |
| Inconsistent binary | `3fad7998-f64b-4fe3-8f43-aa15709f8cc6` | Infrastructure failure; no output/artifacts |
| Valid binary | `cf0b9fcf-cc5b-4736-bba1-8df531c30821` | Infrastructure failure; no output/artifacts |

The public status endpoint confirmed both terminal failures but supplied no underlying cause or
agent-version identifier. No task-interpretation, consistency, experiment, or model-reload check
can be scored from these runs. No claim is made that training did or did not execute internally.

The account balance changed from **$102.086276** to **$102.067010** after the first attempt, then
to **$102.065671** after the second: an observed decrease of **$0.020605**. These are account
balance deltas, not itemized run charges. No funds were added. Public status snapshots are retained
locally under the ignored `.baseline-results/cloud/` directory; there were no artifacts to fetch.

The PR remains a draft. Resolve the service infrastructure failure before resuming the batch;
retain these attempts as failures, and record any replacement runs separately.

## Batch

1. Run each of the six files under `../inputs/` once. Each allows at most eight fits and 600
   cumulative training seconds. Use the platform's default agent model and 2 GiB runtime memory.
2. Run each of the six files under `inconsistent-inputs/` once. The original prose is unchanged,
   while structured quality contradicts its metric, averaging, or seasonal-period requirement.
   These must stop as `inconsistent_inputs` before data inspection or any model training.

This is twelve remote runs. The training ceiling does not cap LLM charges or the full run's cost;
inspect actual usage between runs. The approved batch uses existing credits; no top-ups or
unlimited repeats are authorized.

## Review evidence

For valid inputs, inspect the review, resolved contract, full trial history, final receipt,
evaluation report, and model bundle. Verify appropriate features and splits, real hypotheses
responding to measured evidence, every constraint retained, best feasible selection, successful
artifact reload, and honest stop reasons. `no_feasible_model` is acceptable when supported by the
measurements; a tool or interpretation error is not evidence of infeasibility.

For inconsistent inputs, verify the conflict explanation points to the actual disagreement,
there are no training trials, and no resolved evaluation contract or accepted model artifact.

Record run IDs, agent version/model, costs, failures, and artifact locations. Revisit prompt or
tools when evidence warrants it and track replacement runs against the authorized scope and
remaining allowance. Keep local tool-chain results distinct from autonomous-agent outcomes.
