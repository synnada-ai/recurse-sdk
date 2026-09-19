# Slide-and-collect puzzle rules

The rules this example's simulator (`agent/sim.py`) applies when it certifies a
level. The example keeps the game's core sliding and collecting behavior plus two
extras: obstacles and layered seats.

## Board, seats, and settlers

- The board is a `BoardSize.x` by `BoardSize.y` grid. Cell `(0, 0)` is the
  bottom-left corner; x grows right and y grows up.
- `ObstaclePositions` are permanently blocked cells.
- A **seat** is a colored block covering one to five cells. `SeatID` picks its
  shape from the table below, listed as unrotated `(x, y)` cells.
- A **settler** is a colored figure standing on one cell. Settlers never move.
- At the start, seats do not overlap each other or obstacles, and no settler
  stands on a seat or an obstacle.

| SeatID | Shape | Cells | Anchor |
| --- | --- | --- | --- |
| 0 | single | `(0,0)` | `(0,0)` |
| 1 | domino | `(0,0) (1,0)` | `(0,0)` |
| 2 | line of three | `(0,0) (1,0) (2,0)` | `(1,0)` |
| 3 | T | `(0,0) (1,0) (2,0) (1,1)` | `(1,0)` |
| 4 | plus | `(0,0) (1,-1) (1,0) (1,1) (2,0)` | `(1,0)` |
| 5 | long L | `(0,0) (0,1) (0,2) (1,0)` | `(0,0)` |
| 6 | short L | `(0,0) (0,1) (1,0)` | `(0,0)` |
| 7 | square | `(0,0) (0,1) (1,0) (1,1)` | `(0,0)` |

To place a seat: subtract the anchor from every cell, mirror (`x -> -x`) if
`IsMirrored`, rotate clockwise `Rotation % 4` quarter turns (`(x, y) -> (y, -x)`
each), then add `GridPos`. Mirroring comes before rotation.

## Moving and collecting

1. A move slides one seat exactly one cell up, down, left, or right. A drag in
   the display is just several such moves in a row.
2. The move is legal only if every destination cell is on the board, is not an
   obstacle, is not covered by another seat, and holds no settler of a different
   color. An illegal move changes nothing.
3. After the move, every same-colored settler under the seat boards it. Each
   boarding settler leaves the board and uses one unit of the seat's capacity.
   A seat's capacity equals its cell count. If more settlers are covered than
   capacity remains, board them in ascending `(x, y)` order.
4. A seat at full capacity leaves the board and frees its cells.
5. Settlers beside a seat do nothing. Only covered settlers board.
6. The level is solved when no seats remain.

## Layered seats

- A seat with an inner color (`InnerBrickColorIndex` not `null` and not `-1`) has
  two layers. When its outer layer fills, it does not leave. It stays in
  place, switches to the inner color, and starts again with empty capacity.
- It leaves the board when the inner layer fills. A layered seat therefore collects its
  cell count of each color, outer color first.

## The counting rule

For every color, total seat capacity must equal the number of settlers of that
color. Count a layered seat's cells once for each of its two colors. All
reference levels satisfy this exactly; every generated level must too.

## What the designer produces

Each run designs one level for a target campaign `slot` (default 34). The validator
compares the design with reference levels near that slot: board size, settler
count, free space, and how many settlers start next to their seat must fall inside
the bands that `window` reports. Boards are portrait or square, never landscape.

This example registers tools only for plain seats, obstacles, and layered seats.
The toolkit also knows axis-locked seats, connected seats, locks and keys, and
settler queues; they are left out to keep the loop small. A layered seat must cover
at least two cells, and neither of its colors may be used by another seat.

A level is accepted only if it passes the structural checks, satisfies the counting
rule, and is solved by the independent solver under the rules above. Visual clarity
and enjoyment still require human review.

## Reference format and display

Read `BoardSize`, `Seats`, `Settlers`, and `ObstaclePositions`. Each seat provides
`GridPos`, `ColorIndex`, `InnerBrickColorIndex`, `SeatID`, `Rotation`, and
`IsMirrored`. Each settler provides `GridPos` and `ColorIndex`. Color indices run
0-9.

The reference files come from two editor versions. "No value" appears as `null`
or `-1` for the inner color, `0` or `-1` for `ConnectedBrickID`, `"None"` or `0`
for `AxisLockType`, and `null` or `{"Type": 0, ...}` for `LockNKeyConfig`. No
reference level here uses those last three mechanics. `LevelDuration`,
`CameraLocalPosition`, `LocalScale`, `DifficultyType`, and any other field do not
govern play. Saved levels use the same keys as `Level_a.json`.

Draw row `y = 0` at the bottom. Draw seats as colored wells showing remaining
capacity, mark layered seats and show their inner color, draw settlers as small
figures, and draw obstacles as missing floor. A workable palette for indices 0-9:
`#2fa3a0 #4caf50 #d3382f #f0bc2e #8d6e63 #f48fb1 #c2185b #2b4a9b #2e7d32 #7e57c2`.

These rules were inferred from reference levels and play observation; they do not
certify compatibility with the full original game.
