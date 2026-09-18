# Arrow puzzle game demo rules

This is a deliberately small game specification for a new demo implementation.
It retains the original game's core escape behavior and ice. The supplied JSON
levels are reference data; no original implementation is part of this project.

## Board and arrows

- The board is the finite set of axial hex coordinates `(q, r)` in `tiles`.
- Every tile is ordinary floor. A missing tile is a gap, not an exit hole.
- A arrow is an ordered, non-self-overlapping chain of at least two adjacent hexes.
  `segments[0]` is its head; later segments run toward its tail.
- All segments must be on the board. Arrows cannot overlap each other.
- Arrow IDs must be unique. Color is visual only; it has no gameplay effect.
- The head faces away from its next segment. Its direction vector must equal
  `segments[0] - segments[1]`.

| Direction value | Delta `(dq, dr)` |
| --- | --- |
| 0 | `(0, 1)` |
| 1 | `(1, 0)` |
| 2 | `(1, -1)` |
| 3 | `(0, -1)` |
| 4 | `(-1, 0)` |
| 5 | `(-1, 1)` |

## Tapping and escaping

1. A tap selects one arrow that is still present.
2. If the arrow is frozen, the tap does nothing.
3. Otherwise, inspect the straight ray starting one hex ahead of its head and
   continuing in its facing direction through the last board tile on that ray.
4. Any remaining arrow segment on that ray blocks escape, including a segment of
   the tapped arrow's own body. A blocked tap does not move any arrow or change state.
5. If the ray is clear, remove the entire selected arrow and increment the escaped
   arrow count by one. There is no partial movement or body rotation.
6. Recompute which arrows can escape. The level is solved when no arrows remain.

An interior gap does not end the ray: a arrow on the other side of the gap still
blocks it. If no board tile lies ahead, the ray is clear. All movement in this
demo is the discrete behavior above; animation is presentation only.

## Ice

- An ice feature has an integer `requiredArrow = N`.
- The arrow is frozen while fewer than `N` other arrows have escaped.
- Once the escaped count reaches `N`, it is permanently thawed. Its geometric
  escape path must still be clear before it can leave.
- A frozen arrow occupies and blocks its cells just like any other remaining arrow.
- Counts advance on successful escapes, not taps or elapsed time.
- For generated levels, use `1 <= N < total arrow count`. A legal threshold alone
  does not establish solvability; the whole level must be checked.

In the baseline JSON, ice is represented by a non-null `feature` whose `$type`
names `IceFeatureData`, with `requiredArrows` holding the threshold. `FeatureType`
and the assembly suffix are serialization metadata, not additional mechanics.

## Scope for new levels

Produce two distinct tutorial-scale levels:

| Property | Plain level | Ice level |
| --- | --- | --- |
| Arrows | 5–8 | 5–8 |
| Board tiles | 18–40 | 18–40 |
| Body length | 2–9 hexes | 2–9 hexes |
| Iced arrows | 0 | 1–2 |
| Palette | 3–4 colors | 3–4 colors |

Generated boards must be connected through adjacent hexes and fully occupied by
arrow segments at the start. Choose counts and lengths that can satisfy this.
Internal gaps are allowed, provided the remaining board is connected.

These are proposed demo limits, not the original game's production contract.
Do not apply them retroactively to the baselines: Level 2 has only three arrows,
and Level 12 has six iced arrows and seven colors. Preserve those references.

Do not introduce keys, padlocks, ropes, scissors, chains, holes, pipes, canes,
blocker sheets, TNT, bombs, timers, or other mechanics. Do not import production
requirements for 70–110 arrows, late-game difficulty, or figurative artwork.

## Acceptance and analysis

Every generated level must pass structural checks and have a complete legal
escape sequence. Independently replay that sequence after saving and reloading
the JSON; a generator's claimed solution is not proof.

For the ice level, ice must affect play: demonstrate a reachable state where
removing ice would make a geometrically clear arrow tappable. Report that witness.
Do not equate a frozen badge with a meaningful constraint.

Analyze opening choices, blocking dependencies, and how the available choices
change during a solve. Describe measured differences without inventing a
production difficulty rating. Every successful solution removes every arrow once,
so successful-tap count alone does not measure difficulty. With these monotone
rules, successful escapes cannot make later moves harder; do not invent traps
that require undoing an escape.

Reject copies of a baseline or the other output after translation, rotation,
reflection, ID changes, or recoloring. Transform both positions and facing when
comparing layouts, and retain ice assignments in the comparison. Also inspect
similarity with ice ignored so adding a badge cannot disguise a copied layout.
Report similarity only against this four-level subset and the other output;
it cannot establish originality against all 200 original levels.

Visual clarity and enjoyment still require human review.

## Baseline format and display

Read `levelId`, `tiles`, `arrows`, and `levelFeatures`. Each arrow provides `id`,
`segments`, `direction`, `color`, and optional `feature`. The selected baselines
have empty `levelFeatures`. Camera settings, time limits, difficulty labels,
tutorial flags, and Unity vector metadata do not govern this demo.

Use a flat-top hex grid. To reproduce the documented game orientation, transform
axial coordinates by `(q, r) -> (q, -q-r)` before applying the usual flat-top
projection. Equivalently, for hex radius `s`, screen centers are
`x = 1.5*s*q`, `y = -sqrt(3)*s*(r + q/2)` with screen y increasing downward.
Apply the same transform to head arrows. Color adjacent arrows distinctly where
possible; keep IDs, heads, directions, and ice thresholds legible.

These movement facts were distilled from the legacy strict oracle and the
reviewed game-orientation notes. They define the demo contract; building this
demo does not certify compatibility with the full Unity game.
