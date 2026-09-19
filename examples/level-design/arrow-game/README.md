# Arrow Game

A prompt-only Recurse example. It ships a game specification, four reference levels, and one
prompt. A coding assistant with the Recurse skill reads them, builds a level-design agent from
scratch, runs it, and shows you two new levels it designed.

In the game, arrows lie on a hexagonal board. Tapping an arrow removes it if the straight line
ahead of its head is clear; otherwise nothing happens. Iced arrows stay frozen until enough other
arrows have escaped. The level is solved when no arrows remain. [rules.md](rules.md) has the full
rules and the level format.

## When to use it

Use it to see what the Recurse skill does with nothing but a specification and a few examples:
the assistant writes the simulator, the action and validator tools, the agent prompt, and a
gallery, then deploys the agent. There is no agent code in this folder, so every run builds its
own. [Slide-and-Collect Game](../slide-collect-game) and
[Conveyor Shooter Game](../conveyor-shooter-game) are the opposite case:
ready-made agents you can run directly.

## Run it

1. Install the Recurse CLI and log in:

   ```sh
   uv tool install recurse-sdk
   recurse login
   ```

2. Open this folder as the project in a coding assistant that has the
   [Recurse skill](https://recurse.run/SKILL.md) installed.
3. Give it the contents of [guidance.md](guidance.md) as the prompt.

The assistant's Recurse runs happen in the Recurse cloud and consume credits. The prompt sets a
10-15 minute target for the whole session and tells the assistant to stop at the deadline and
report what exists.

## Inputs

| File | Role |
| --- | --- |
| [guidance.md](guidance.md) | The prompt: what to build, what to demonstrate, what to deliver, and the time limit |
| [rules.md](rules.md) | Board, tapping, ice, the limits for the two new levels, acceptance checks, similarity checks, and display conventions |
| [corpus/](corpus/) | Four existing levels as reference data |

| Reference level | Tiles | Arrows | Iced arrows | Shows |
| --- | --- | --- | --- | --- |
| [Level_a.json](corpus/Level_a.json) | 19 | 3 | 0 | Basic blocking and curved bodies |
| [Level_b.json](corpus/Level_b.json) | 18 | 4 | 0 | Varied facing directions and an interior gap |
| [Level_c.json](corpus/Level_c.json) | 23 | 5 | 0 | A larger basic dependency puzzle |
| [Level_d.json](corpus/Level_d.json) | 35 | 7 | 6 | Ice thresholds tied to escaped-arrow counts |

The reference files keep every tile, arrow, direction, color, and ice threshold of the original
levels. Only names were made generic: the file names, the `arrows` key, and the type namespace.
They are licensed reference data excluded from Git by the local `.gitignore`. An ordinary Git
checkout does not include them; transfer the corpus separately.

The prompt asks the assistant to treat this folder as the project root and to use only these
files as game material. Filesystem permissions decide whether that boundary is enforced.

## How the loop works

The prompt leaves the design to the assistant but fixes the division of labor:

1. **Analyze.** The assistant studies the reference levels' blocking patterns, opening choices,
   arrow shapes, and ice behavior.
2. **Build.** It writes a Recurse agent with action tools that apply the agent's layout choices
   and independent validator tools that replay and measure a level. The coding assistant builds
   the specialist; the specialist makes the level-design decisions.
3. **Iterate.** The agent designs one plain level and one ice level within the limits in
   `rules.md`, revising from validator feedback. At least one genuine revision must be shown, with
   before and after boards and the reason it was kept or rejected.
4. **Deploy.** The assistant deploys the agent as MCP and verifies one invocation with a fresh
   level brief.

## Result

A session that goes well leaves you with:

- a short analysis of the reference levels and the freshly built agent;
- two new level JSON files with validation evidence and playable solution replays;
- a local interactive gallery showing the reference and new levels, with click-to-play, reset,
  solution stepping, readable arrow heads and ice counters, and the revision history;
- a working MCP deployment with connection details and one verified invocation.

Check the result yourself: each new level should have 5-8 arrows on 18-40 tiles, fully cover a
connected board, replay to a solved board after reloading the saved JSON, and differ from the
references and from the other new level under rotation, reflection, and recoloring. In the ice
level, the ice must change what can be tapped at some reachable moment. The assistant should
report elapsed time and anything it did not finish rather than extending the attempt.

Because every session writes its own implementation, results vary between runs and between
assistants.

## Limitations

This is a deliberately small specification. It keeps the core escape rule and ice, and excludes
every other mechanic of the original game. Similarity is checked only against these four levels
and the other output, so it cannot establish originality against a full level set. The rules were
distilled from notes on the original game; building this example does not certify compatibility
with it. Visual clarity and enjoyment still require human review.
