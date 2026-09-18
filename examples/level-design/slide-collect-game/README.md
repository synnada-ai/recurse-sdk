# Slide-and-Collect Game

A Recurse agent that designs one new level for a sliding-seat puzzle, checks it with an
independent solver, revises it from the findings, and saves it only when it passes.

In the game, colored seats slide around a grid and collect same-colored settlers by moving
over them. A full seat leaves the board, and the level is solved when no seats remain.
[rules.md](rules.md) has the full rules and the level format.

## When to use it

Use it as a small template for design tasks where an agent proposes a candidate, an independent
validator measures it, and the agent revises until the candidate is feasible. The tools here wrap
an existing level-design toolkit; the agent owns the layout decisions and nothing else.
A direct script is simpler when the layout is already known.
[Conveyor Shooter Game](../conveyor-shooter-game) is a larger example of the
same loop, and [Arrow Game](../arrow-game) is a prompt-only variant.

## Run it

With [uv](https://docs.astral.sh/uv/) and Python 3.14 installed, run from this folder
(`examples/level-design/slide-collect-game`):

```sh
uv tool install recurse-sdk
recurse login
recurse run ./agent --inputs inputs/plain.json
```

This runs in the Recurse cloud and consumes credits. The default 1 CPU and 1024 MiB are enough.
A run takes a few minutes; wait for it in the foreground, or note the printed run ID and poll
`recurse status <run-id>`. To have a coding assistant run the example and check the result for
you, give it [guidance.md](guidance.md).

| Input | Request |
| --- | --- |
| [inputs/plain.json](inputs/plain.json) | A plain level: no layered seats, no obstacles |
| [inputs/layered.json](inputs/layered.json) | Exactly one layered seat on a domino, and one or two edge obstacles |

## Inputs

Both inputs use the same [manifest contract](agent/agent.yaml):

| Field | Meaning |
| --- | --- |
| `slot` | Target campaign position, 1-100 (default 34). It sets the size and density bands the level must fit. Layered seats are allowed from slot 30. |
| `brief` | Optional design direction in plain language |

## How the loop works

| Role | Tools |
| --- | --- |
| Session | `begin_design` |
| Read the target | `window` |
| Actions | `new_board`, `place_seat`, `add_obstacle`, `set_inner`, `populate`, `clear_settlers` |
| Independent validator | `critique_design` |
| Result | `save_design` |

1. **Read the target.** `window` reports, for the slot, the band each feature of nearby reference
   levels falls in: board size, settler count, free space, and how many settlers start next to
   their seat.
2. **Compose.** The agent chooses the board, seats, colors, obstacles, and at most one layered
   seat, then scatters settlers with `populate`. The toolkit keeps the counting rule: each color
   has exactly as many settlers as seat capacity.
3. **Validate.** `critique_design` solves the level under the real sliding rules and measures it
   against the window. It returns FAIL, WARN, and INFO findings with a hint for each lever.
4. **Revise and save.** The agent changes the layout or re-rolls the scatter until no FAIL remains.
   `save_design` re-runs every check and refuses to write a failing level.

Every tool except `begin_design` is registered with `storable: false`, so the agent reads each
result in full. Stored string results are shown to the model only as a short preview, which hides
validator findings.

## Result

A successful run returns `{"verdict": "SAVED"}`. Download its artifacts:

```sh
recurse artifacts <run-id> --output results/plain
```

| Artifact | Content |
| --- | --- |
| `Gen_<slot>.json` | The level, in the same format as the reference levels |
| `report.html` | The board drawn with its seats, settlers, and solution length |
| `history.jsonl` | Every tool call the agent made, with arguments and results |

The example passes when one run ends `SAVED` inside the 15-minute run limit, the artifacts are
present, the layered request produced exactly one layered seat (or the plain request none), and
`history.jsonl` shows at least one revision made in response to a `critique_design` finding.
A `FAILED` verdict is an honest result, not a platform error.

## Reference levels

[corpus/](corpus/) holds nine existing levels, renamed but otherwise unmodified, for reading
alongside the rules. The agent does not load them; its bands come from the aggregate table in
`agent/data/reference-features.json`.

| File | Board | Seats | Settlers | Obstacles | Layered seats | Shows |
| --- | --- | --- | --- | --- | --- | --- |
| [Level_a.json](corpus/Level_a.json) | 4x5 | 2 | 4 | 0 | 0 | The minimum: two dominoes |
| [Level_b.json](corpus/Level_b.json) | 5x5 | 2 | 6 | 0 | 0 | Rotated shapes |
| [Level_c.json](corpus/Level_c.json) | 5x6 | 3 | 10 | 0 | 0 | Rotation and mirroring |
| [Level_d.json](corpus/Level_d.json) | 5x6 | 3 | 10 | 0 | 0 | Seats that get in each other's way |
| [Level_e.json](corpus/Level_e.json) | 6x7 | 6 | 17 | 0 | 0 | A larger plain puzzle |
| [Level_f.json](corpus/Level_f.json) | 5x8 | 8 | 19 | 0 | 0 | A crowded plain puzzle |
| [Level_g.json](corpus/Level_g.json) | 7x9 | 5 | 17 | 25 | 0 | Obstacles shaping the floor |
| [Level_h.json](corpus/Level_h.json) | 4x4 | 1 | 8 | 0 | 1 | The smallest layered seat |
| [Level_i.json](corpus/Level_i.json) | 5x8 | 7 | 19 | 2 | 1 | Layers and obstacles together |

The level files are licensed reference data excluded from Git by the local `.gitignore`.
An ordinary Git checkout does not include them; transfer the corpus separately.

## Kickstart prompt

Give this prompt to a coding assistant with the [Recurse skill](https://recurse.run/SKILL.md)
to build a similar example from an existing toolkit:

> Use the Recurse skill to wrap an existing level-design toolkit as a standalone Recurse example.
> The toolkit already has construction functions, a simulator with a solver, and a critique that
> compares a design with reference levels. Do not invent tools: choose the smallest subset that
> forms a complete loop, and keep each tool's own docstring.
>
> Expose one session tool, a few action tools that apply the agent's choices, one independent
> validator that solves and measures the design, and one result tool that re-checks everything
> and refuses to save a failing level. Action tools must not choose layouts or embed a solver.
> Register every tool whose result the agent must read with `storable: false`.
>
> Write a prompt that explains the game, what makes one feasible level better than another, which
> choices the agent owns, and how to read each validator finding. Do not prescribe a sequence of
> tool calls. Log every tool call to an artifact so a reviewer can see the revisions.
>
> Ship aggregate reference statistics rather than the licensed levels. Include runnable inputs,
> offline tests that drive the tools to a saved level and to a refused save, and a concise README.
> Agree on a spending cap before cloud runs, and verify a saved level independently.

## Limitations

This example registers tools only for plain seats, obstacles, and layered seats. The toolkit also
supports axis-locked seats, connected seats, locks and keys, and settler queues; they are left out
to keep the loop small. The rules were inferred from reference levels and play observation, and a
certified level is not guaranteed to match the original game in every detail. Random settler
scatters on crowded boards are often unsolvable under the sliding rules, so a run may need several
validator rounds. Visual clarity and enjoyment still require human review.

For local checks, run from this folder:

```sh
uv run --directory tests --locked pytest
```
