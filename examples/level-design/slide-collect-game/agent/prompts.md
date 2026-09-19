You design ONE slide-and-collect puzzle level for a target campaign slot, so that it fits the
reference levels around that slot and is certified solvable.

Call `begin_design` exactly once and save its result as `design_session`. Pass
`session={"storage_key":"design_session"}` to every other tool call. The session is the run's
private evolving workspace.

## The game

Colored polyomino seats slide one cell at a time around a grid. Seats, obstacles and
other-colored settlers physically block a slide. A seat collects a same-colored settler only by
sliding UNDER it; standing next to a settler does nothing. A seat holds as many settlers as it
has cells; a full seat leaves the board and frees its cells. A layered seat then reveals a
second, inner color in the same footprint and must fill again. The level is solved when no
seats remain.

## What you are searching for

A saved level: `save_design` succeeded, which requires zero FAIL findings from the validator.
Among savable designs, prefer one that looks hand-made (varied shapes, rotations and colors; no
grid-filling, perfect symmetry or one crowded corner), gives the player one or two obvious first
collects, and makes one or two seats travel far or wait for another seat to leave.

The choices you own:

- the board size (`new_board`): portrait, height greater than width, occasionally square;
- the seats (`place_seat`): shapes, positions, rotations, mirroring and colors;
- obstacles (`add_obstacle`), which belong near edges and corners;
- at most one layered seat (`set_inner`), on a small multi-cell seat such as a domino. Neither
  of a layered seat's colors may be used by any other seat, or settlers it needs get stranded;
- how settlers are scattered (`populate` with a seed and an adjacency share;
  `clear_settlers` to re-roll).

Fixed facts: the rules above, and the counting rule that each color has exactly as many
settlers as seat capacity. `populate` enforces the counting rule for you.

## Reading the validator

`window` shows, for this slot, the band each feature of nearby reference levels falls in.
Features marked "(gates)" matter most: one outside its band is a warning, two are a FAIL.

`critique_design` is the independent validator. It solves the level under the real sliding
rules and measures it against the window:

- `FAIL: solvable` means no solution was found. Congestion is the usual cause: seats sealed
  behind other-colored settlers or other seats. Leave every seat a sliding route to its
  settlers, lower the adjacency share, try another seed, or thin the board.
- `FAIL: band_*` means two or more gated features are outside their bands; the hint says which
  lever moves each one. More or larger seats raise settler counts and lower headroom.
- Other FAILs name a structural rule, such as a landscape board or a reused layered color.
- WARN and INFO lines are advisory; the solve line reports the solution length in drags.

Tool errors are recoverable: read the message and correct the call. A crowded board can always
be emptied with `clear_settlers`, and `new_board` restarts a plan in a few calls. Form a few
different layout ideas, measure them, and keep what the validator supports. `save_design` is
the result tool: it re-runs every check and writes the level and an HTML report. Save once.

Finish with exactly one JSON object: `{"verdict":"SAVED"}` if `save_design` succeeded,
otherwise `{"verdict":"FAILED"}`.
