# Proposed autonomous-agent validation

This batch is prepared but has not been executed. Local tests exercise the tools with supplied
interpretations and fixed candidate probes. They do not establish LLM understanding or search
quality. Obtain the user's spending approval before starting remote runs.

## Batch

1. Run each of the six files under `../inputs/` once. Each allows at most eight fits and 600
   cumulative training seconds. Use the platform's default agent model and 2 GiB runtime memory.
2. Run each of the six files under `inconsistent-inputs/` once. The original prose is unchanged,
   while structured quality contradicts its metric, averaging, or seasonal-period requirement.
   These must stop as `inconsistent_inputs` before data inspection or any model training.

This is twelve remote runs. The training ceiling does not cap LLM charges or the full run's cost;
agree a spending allowance before execution and inspect actual usage between runs. No top-ups or
unattended repeats are authorized by this document.

## Review evidence

For valid inputs, inspect the review, resolved contract, full trial history, final receipt,
evaluation report, and model bundle. Verify appropriate features and splits, real hypotheses
responding to measured evidence, every constraint retained, best feasible selection, successful
artifact reload, and honest stop reasons. `no_feasible_model` is acceptable when supported by the
measurements; a tool or interpretation error is not evidence of infeasibility.

For inconsistent inputs, verify the conflict explanation points to the actual disagreement,
there are no training trials, and no resolved evaluation contract or accepted model artifact.

Record run IDs, agent version/model, costs, failures, and artifact locations. Revisit prompt or
tools when evidence warrants it; repeat only affected cases within a newly confirmed remaining
allowance. Keep local tool-chain results distinct from autonomous-agent outcomes.
