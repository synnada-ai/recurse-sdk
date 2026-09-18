# Conveyor Shooter Game

A Recurse agent that turns a source image into one playable puzzle level with keys and locks,
checks it with an independent solver, revises it from the findings, and saves it only when a
winning replay exists.

In the game, colored shooters ride a conveyor belt around a pixel picture and destroy pixels of
their own color. The level is won when the picture is gone and lost when the waiting tray
overflows. [rules.md](rules.md) has the full rules and the level format.

## When to use it

Use it as a template for design tasks that mix a visual judgment with a hard feasibility check:
the agent decides how the picture is composed and where mechanics go, and a solver decides
whether the result can be played. The tools wrap an existing level-design toolkit; the agent
owns the design choices and nothing else.
[Slide-and-Collect Game](../slide-collect-game) is a smaller introduction to
the same loop, and [Arrow Game](../arrow-game) is a prompt-only variant.

## Run it

With [uv](https://docs.astral.sh/uv/) and Python 3.14 installed, run from this folder
(`examples/level-design/conveyor-shooter-game`):

```sh
uv tool install recurse-sdk
recurse login
recurse run ./agent --inputs inputs/dragon.json --cpu 2 --memory-mib 4096
```

This runs in the Recurse cloud and consumes credits. **The memory setting is required:** the
solver search needs about 2 GiB, and with the default 1024 MiB the run ends
`infrastructure_failed` instead of returning a verdict. Pass the same `--cpu` and `--memory-mib`
values to `recurse deploy`. A run takes about five minutes; wait for it in the foreground, or note the
printed run ID and poll `recurse status <run-id>`. To have a coding assistant run the example and check the result for
you, give it [guidance.md](guidance.md).

| Input | Source image |
| --- | --- |
| [inputs/dragon.json](inputs/dragon.json) | [sources/dragon.png](sources/dragon.png) |
| [inputs/peacock.json](inputs/peacock.json) | [sources/peacock.png](sources/peacock.png) |
| [inputs/jellyfish.json](inputs/jellyfish.json) | [sources/jellyfish.png](sources/jellyfish.png) |

The images are Microsoft Fluent Emoji assets under the MIT license in
[sources/LICENSE](sources/LICENSE). Each input file embeds its image as base64 (about 50 KB), so
do not print the input files into an assistant's context.

## Inputs

All inputs use the same [manifest contract](agent/agent.yaml):

| Field | Meaning |
| --- | --- |
| `source_image_base64` | The source PNG, as raw base64 or a data URI. Required. |
| `brief` | Optional visual or gameplay direction in plain language |
| `width`, `height` | Board size in cells: 20-35 wide (default 35) and 22-40 tall (default 40) |
| `crop` | Optional `[left, top, right, bottom]` pixel crop of the source image |

To use your own image, base64-encode a PNG with a clear subject on a plain or transparent
background and put it in `source_image_base64`.

## How the loop works

| Role | Tools |
| --- | --- |
| Session | `begin_design` |
| Choose the picture | `extract_image_variants`, `look_image_variant`, `compare_image_variants`, `freeze_image_baseline` |
| Actions | `start_from_source`, `apply_composition_school`, `inspect_source_coordinates`, `place_key` |
| Visual review | `look`, `review_visual_retention` |
| Independent validator | `configure_gameplay` |
| Result | `save_candidate` |

1. **Choose the picture.** The agent compares deterministic pixel conversions of the image and
   freezes one as immutable source art.
2. **Compose.** The frozen picture alone is usually too sparse to play, so the agent surrounds it
   with gameplay pixels using one of four composition schools, then places two to six 2x2 keys
   inside the picture. A key reachable from a board edge at the start is rejected.
3. **Validate.** `configure_gameplay` builds four shooter lanes with one lock per key, searches
   for a win, replays it, and measures the play pressure: relaunches, tray use, and available
   actions. It reports `solved`, `violations`, and `meets_save_gate`.
4. **Revise and save.** The agent changes key positions, key count, composition, or lane
   treatment, one idea at a time. `save_candidate` re-certifies and refuses a level that cannot be
   won or whose keys and locks never take part in the win.

Every tool except `begin_design` is registered with `storable: false`, so the agent reads each
result in full. Stored string results are shown to the model only as a short preview; with that
preview the agent never saw the validator's `violations` and repeated the same layout until the
run timed out.

## Result

A successful run returns `{"verdict": "SAVED"}`. Download its artifacts:

```sh
recurse artifacts <run-id> --output results/dragon
```

| Artifact | Content |
| --- | --- |
| `level_151_1.json` | The level, in the same format as the reference levels |
| `level_151_1.certificate.json` | The winning action sequence and the solver's search record |
| `level_151_1.mechanic-trace.json` | When each key was collected and each lock opened |
| `level_151_1.metrics.json` | Measurements against the reference bands |
| `report.html`, `source-comparison.html` | The playable board, and the source beside it |
| `frozen_art.json`, `source-art-report.html` | The frozen pixel conversion of the source image |
| `diagnostics/` | A snapshot of each candidate and the toolkit's own event trace |
| `history.jsonl` | Every tool call the agent made, with arguments and results |

The example passes when one run ends `SAVED` inside the 15-minute run limit, the artifacts are
present, the certificate replays to a win with every key collected and every lock opened, and
`history.jsonl` shows at least one revision made in response to a `configure_gameplay` result.
Whether the picture in `source-comparison.html` is still recognizable needs a human look.
A `FAILED` verdict is an honest result, not a platform error.

## Reference levels

[corpus/](corpus/) holds ten existing levels, renamed but otherwise unmodified, for reading
alongside the rules. The agent does not load them; its bands come from the aggregate
measurements in `agent/data/campaign-context.json`. The files are large because each pixel is
its own record, so load them with code rather than reading them as text.

| File | Board | Pixels | Colors | Lanes | Shows |
| --- | --- | --- | --- | --- | --- |
| [Level_a.json](corpus/Level_a.json) | 16x15 | 180 | 2 | 2 | The minimum |
| [Level_b.json](corpus/Level_b.json) | 20x20 | 230 | 3 | 2 | A sparse picture |
| [Level_c.json](corpus/Level_c.json) | 20x20 | 400 | 3 | 3 | A full board |
| [Level_d.json](corpus/Level_d.json) | 20x20 | 400 | 3 | 3 | Deeper lanes |
| [Level_e.json](corpus/Level_e.json) | 20x20 | 400 | 5 | 3 | More colors |
| [Level_f.json](corpus/Level_f.json) | 20x20 | 400 | 6 | 4 | Four lanes |
| [Level_g.json](corpus/Level_g.json) | 20x22 | 400 | 4 | 3 | A non-square board |
| [Level_h.json](corpus/Level_h.json) | 21x21 | 340 | 6 | 3 | Many colors, sparse picture |
| [Level_i.json](corpus/Level_i.json) | 20x20 | 220 | 5 | 3 | Four keys and four locks |
| [Level_j.json](corpus/Level_j.json) | 25x28 | 680 | 5 | 4 | Five keys and locks on a larger board |

The level files are licensed reference data excluded from Git by the local `.gitignore`.
An ordinary Git checkout does not include them; transfer the corpus separately.

## Kickstart prompt

Give this prompt to a coding assistant with the [Recurse skill](https://recurse.run/SKILL.md)
to build a similar example from an existing toolkit:

> Use the Recurse skill to wrap an existing image-to-level design toolkit as a standalone Recurse
> example. The toolkit already converts an image to a pixel grid, composes a board around it,
> places mechanics, and certifies a level with a game engine and solver. Do not invent tools:
> choose the smallest subset that forms a complete loop, and keep each tool's own docstring.
>
> Expose one session tool, action tools that apply the agent's choices, a visual review step, one
> independent validator that solves, replays, and measures the level, and one result tool that
> re-certifies and refuses to save a level that cannot be won. Action tools must not choose
> placements or embed a solver. Leave out tools that return large images. Register every tool
> whose result the agent must read with `storable: false`.
>
> Write a prompt that explains the game, what makes one feasible level better than another, which
> choices the agent owns, and how to read each validator field. Do not prescribe a sequence of
> tool calls. Log every tool call to an artifact so a reviewer can see the revisions.
>
> Measure the tool chain's peak memory and time locally before any cloud run, and document the
> resources the run needs. Ship aggregate reference measurements rather than the licensed levels,
> and redistributable source images with their license. Include runnable inputs, offline tests
> that drive the tools to a certified save and to a refused save, and a concise README. Agree on a
> spending cap before cloud runs, and verify a saved level independently.

## Limitations

This example registers tools only for keys, locks, and connected shooters, and always builds four
lanes on a board of at least 20x22. The toolkit and engine also support hidden shooters, pixel
pipes, and egg boxes; they are left out to keep the loop small. The fast solver check stops after
5,000 states, so layouts with more keys often fail to certify and the agent may fall back to two
keys. The rules were inferred from reference levels and play observation, and a certified level is
not guaranteed to match the original game in every detail. Recognizability is an LLM judgment
recorded by `review_visual_retention`; it still requires human review.

For local checks, run from this folder:

```sh
uv run --directory tests --locked pytest
```
