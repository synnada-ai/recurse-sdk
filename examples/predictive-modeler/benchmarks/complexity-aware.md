
## Hypothesis: spend trials on plausible feasible candidates

Before choosing an experiment, distinguish a failure of predictive quality from a failure of
model size or required inputs. Use the measured failure to explain which configuration change
could address it and what predictive trade-off to expect.

An input-column ceiling can rule out a configuration before fitting: choose an eligible subset
within the ceiling. A byte ceiling cannot be established from a family name alone; use actual
measurements. If size is the objective, consider reducing representation size once a predictive
floor is met, while preserving the best feasible candidate. Changes to vocabulary, tree count,
feature selection, regularization, or family should have a task-specific hypothesis rather than
being attempted as a checklist. Do not assume fewer input columns necessarily means fewer bytes.

Within a short allowance, prefer comparisons that could change the selected feasible model.
Explain why the next experiment is worth its remaining cost. Keep all existing review, split,
measurement, feasibility, and finalization rules unchanged.
