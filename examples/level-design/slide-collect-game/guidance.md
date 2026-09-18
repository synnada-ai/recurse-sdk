# Slide-and-collect puzzle example — guidance prompt

Run this folder's Recurse example end to end and report whether it meets the pass
criteria in the Result section of `README.md`. Use the installed `recurse:recurse` SKILL.md for all
Recurse guidance; do not use website documentation.

Treat this folder as the project root and do not search outside it.

A run takes several minutes. Wait for `recurse run` in the foreground with a long
command timeout; do not background it and end your turn. If the wait is cut short,
the hosted run continues: note the printed run ID and poll `recurse status <run-id>`.

1. Read `README.md`, `rules.md`, and `agent/prompts.md`, and skim `agent/tools.py`.
2. Confirm `recurse login` works, then run `./agent` with `inputs/plain.json` and
   with `inputs/layered.json`. The two runs are independent; run them concurrently.
3. Download each run's artifacts into `results/` and open each `report.html`.
4. Check every pass criterion and report it as met or not met, with
   the run IDs, elapsed time, and the saved level's measured facts.

If a run ends `FAILED` or times out, inspect what the validator reported and
improve the example within these limits: edit `agent/prompts.md`, tool docstrings,
or the inputs. Do not add tools, embed a solver or layout generator in an action
tool, or weaken `critique_design` or `save_design`. Compare any change against the
unchanged run on the same input before keeping it.

Keep the corpus untracked and private. Stop after at most three runs per input and
report what exists rather than extending the attempt.
