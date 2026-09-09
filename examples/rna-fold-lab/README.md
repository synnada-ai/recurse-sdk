# RNA Fold Lab

RNA Fold Lab searches for an RNA sequence whose predicted minimum-free-energy structure matches a
requested dot-bracket structure while respecting fixed bases, GC-content bounds, and a
homopolymer ceiling.

## When to use it

Use this example to study a constrained design problem where plausible constructions can behave
differently under an independent scientific model. The specialist must measure and revise rather
than infer success from complementary base pairs alone. Trivial structures that fold correctly on
the first candidate do not justify Recurse; call the forward-fold oracle directly instead.

## How the loop works

1. `create_sequence` constructs a candidate and checks the declared sequence constraints.
2. `evaluate_sequence` uses the locked ViennaRNA forward-fold oracle to predict the candidate's
   fold and measure its base-pair distance from the target.
3. The specialist compares target and predicted pairs, changes a small number of implicated bases,
   and measures another distinct candidate.
4. `save_best_sequence` writes the best measured candidate and chronological history to
   `best-sequence.json`.

Distance zero means the predicted fold exactly matches the requested structure.

## Run it

Create `inputs.json`:

```json
{
  "target_structure": "((((((....))))))",
  "sequence_template": "NNNNNNNNNNNNNNNN",
  "max_trials": 6
}
```

Then run it once or deploy it as a reusable MCP tool:

```sh
recurse run examples/rna-fold-lab --inputs inputs.json
recurse deploy examples/rna-fold-lab --as mcp
```

## Result

The successful result reports the saved sequence, predicted structure, minimum free energy,
base-pair distance, GC fraction, and measured-trial count. The run also produces
`best-sequence.json` with the full measured history.

## Limitations

This example demonstrates predicted RNA secondary structure. It does not establish biological
function, wet-lab viability, folding kinetics, pseudoknots, molecular interactions, or therapeutic
effectiveness.
