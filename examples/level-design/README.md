# Level Design

Three examples that design puzzle-game levels with Recurse. Each one pairs an agent that makes
the design decisions with an independent validator that solves and measures the level.

| Example | Kind | What it shows |
| --- | --- | --- |
| [arrow-game](arrow-game) | Prompt only | A coding assistant builds the whole agent from a specification and a prompt |
| [slide-collect-game](slide-collect-game) | Runnable agent | A small propose, validate, revise loop over an existing toolkit |
| [conveyor-shooter-game](conveyor-shooter-game) | Runnable agent | The same loop with an image input, a visual review step, and a heavier solver |

## When to use it

Use these examples when a design has hard feasibility checks, such as solvability, and open-ended
choices that benefit from trial and revision. A direct implementation is simpler when the layout
is already known. Start with `slide-collect-game`; read `arrow-game` to see how much the Recurse
skill builds from a specification alone.

## How the loop works

The agent applies its choices through action tools, an independent validator solves the level
and returns findings, and the agent revises until the validator reports no failure. One result
tool re-checks everything and refuses to save a failing level. Tools whose results the agent must
read are registered with `storable: false`, and every tool call is logged to a `history.jsonl`
artifact.

## Run it

Each example's README has its own commands, inputs, and resource requirements. The runnable agents
start with `recurse run ./agent --inputs inputs/<name>.json` from the example's folder.

## Result

A successful run returns `{"verdict": "SAVED"}` with the level file, a visual report, and the
tool-call history as artifacts.

## Limitations

The reference levels these examples describe are licensed data and are not part of this
repository. The runnable agents do not need them; the prompt-only example does.
