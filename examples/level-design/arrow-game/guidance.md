# Arrow puzzle demo — guidance prompt

Build and execute an arrow puzzle level-design demo in 10–15 minutes. Read `rules.md` and the four levels in `corpus/`.
Use the installed `recurse:recurse` SKILL.md for all Recurse implementation and
operation guidance; do not use website documentation. Use `gpt-5.6-luna` for
the Recurse agent.

Treat this folder as the project root. Use only its rules and corpus as game
material, write the implementation afresh, and do not search outside the folder.
The installed skill and development tools are the platform-support exception.

## What to demonstrate

Analyze the existing levels' blocking patterns, opening choices, arrow shapes, and
ice behavior. Use that analysis to build a Recurse agent that designs two new
levels: one plain and one with ice, within the limits in `rules.md`.

The demonstration is about iterative level design. The Recurse agent must choose
layouts, arrow paths, directions, and ice settings, then revise its designs using
feedback from action and independent validator tools. Show at least one genuine
feedback-driven revision, including the before/after boards, measured changes,
and why the agent kept or rejected the revision. The coding agent builds the
specialist; the specialist makes the level-design decisions.

Aim for clear, layered blocking puzzles with meaningful ice and layouts distinct
from the references and each other. Use the acceptance criteria in `rules.md`;
playability alone does not establish an interesting design.

## What to deliver

- A brief corpus analysis and the freshly built Recurse agent.
- Two new level JSON files with validation evidence and playable solution replays.
- A local interactive gallery showing the four reference levels and the new
  levels, clearly labeled. Include click-to-play, reset, solution stepping,
  readable arrow heads and ice counters, and the real revision history.
- A working MCP deployment, connection details, and a verified invocation with
  a fresh level brief. Trials may use direct runs; the MCP invocation can produce
  one of the two demo levels.

Keep the corpus untracked and private; it may accompany the private Recurse
application. Open the gallery for inspection. Check that blocked taps, ice
unlocking, reset, and solution playback agree with the rules.

The 15-minute allowance covers analysis, building, trials, MCP deployment and
invocation, and presentation. Stop at that deadline and show what exists. Report
elapsed time and any missing levels, iteration evidence, verification, or MCP
functionality rather than extending the attempt.
