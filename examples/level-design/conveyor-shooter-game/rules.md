# Conveyor shooter puzzle rules

The rules this example's engine (`agent/conveyor_game/`) applies when it certifies
a level. The example keeps the game's core conveyor-and-shoot behavior plus keys
and locks and connected shooters.

## Board and shooters

- The board is a `width` by `height` grid holding a pixel picture. Cell `(0, 0)`
  is the bottom-left corner; x grows right and y grows up.
- Each entry of `pixels` is one colored cell: `x`, `y`, and a color id `material`.
  Cells without a pixel are empty. `areaX` and `areaY` are always 1 here.
- Shooters wait in lanes (`QueueGroup.shooterQueues`). Each shooter has an `id`,
  a color `material`, and an `ammo` count. Only the front shooter of a lane can
  be launched.
- A conveyor belt runs around the outside of the board. The **tray** holds
  shooters that finished a lap with ammo left; it has `SlotCount` slots.

## The lap

The belt has `2 * (width + height)` positions. A shooter enters at position 0 and
visits them in order, anticlockwise from the bottom-left:

| Positions | Edge | Travel | Fires |
| --- | --- | --- | --- |
| `0 .. width-1` | bottom | left to right, column `x = 0, 1, ...` | up the column |
| next `height` | right | bottom to top, row `y = 0, 1, ...` | left along the row |
| next `width` | top | right to left, column `x = width-1, ...` | down the column |
| last `height` | left | top to bottom, row `y = height-1, ...` | right along the row |

## Playing

1. The player may launch the front shooter of any lane, or any shooter in the
   tray, whenever fewer than `ConveyorLimit` shooters are on the belt. A launched
   shooter starts at position 0. Shooters do not collide or block each other.
2. Time advances in ticks. On each tick, every shooter on the belt fires once
   from its current position and then moves to the next position. Process
   shooters furthest along the belt first.
3. A shot travels in a straight line from the board edge. It stops at the first
   remaining pixel or key. If that is a pixel of the shooter's color, the pixel
   is destroyed and the shooter loses one ammo. Otherwise nothing happens: a shot
   never passes through a pixel, and a wasted shot costs no ammo.
4. A shooter whose ammo reaches 0 leaves the belt at once.
5. A shooter that passes the last position with ammo left moves to the tray. If
   the tray has no free slot, the level is **lost**.
6. The level is **won** when no pixels remain.

## Keys and locks

- A key occupies a block of empty cells (`keys.Keys[].GridPoints`, with uppercase
  `X` and `Y`). Until it is used it blocks shots like a pixel of no color.
- A lock is a queue entry whose `id` is listed in `Locks.Shooters`. It is not a
  shooter: its `material` and `ammo` are placeholders. A lock at the front of a
  lane blocks every shooter behind it.
- A key is **collected** when a shot destroys a pixel directly next to the key,
  when a shot destroys a pixel and the key is the next thing along that same
  line, or when a shooter fires along a line where the key comes first.
- A collected key opens one lock that is at the front of a lane (lowest lane
  first): the lock and the key both disappear. If no lock is at a lane front, the
  key waits, still blocking shots, until one arrives.
- A key must not be reachable by any shot at the start of a level.

## The counting rule

For every color, total shooter ammo must equal the number of pixels of that color
(lock entries do not count). All reference levels satisfy this exactly, and the
designer's toolkit computes shooter ammo so generated levels do too. Because only
matching hits cost ammo, a level is never lost by wasting ammo; it is lost by
filling the tray. Difficulty comes from which colors are exposed when, and from
the order shooters sit in their lanes.

## Connected shooters

Two shooters in different lanes can be connected. They launch together, only when
both are at the front of their lanes, and travel one position apart. The designer
always builds four lanes and at least one connected pair.

## What the designer produces

Each run turns one source image into one level on a board 20-35 wide and 22-40
tall. The picture is a fixed pixel conversion of the image, surrounded by extra
gameplay pixels so the board is dense enough to play. Two to six 2x2 keys sit
inside the picture, with one queue lock per key.

This example registers tools only for keys, locks, and connected shooters. The
toolkit and engine also know hidden shooters, pixel pipes, and egg boxes; they are
left out to keep the loop small.

A level is accepted only if the independent solver finds a win that replays after
reloading, every key is collected and every lock opened during that win, no key
is reachable at the start, and the measured play pressure (relaunches, tray use,
available actions) sits inside the bands of the reference levels. Whether the
picture still reads is a human judgment.

## Reference format and display

Read `SlotCount`, `ConveyorLimit`, `QueueGroup.shooterQueues[].shooters`,
`Locks.Shooters`, and inside `PixelImageData`: `width`, `height`, `pixels`, and
`keys`. `Difficulty`, time limits, `contentHash`, `isValid`, `physicalWidth`,
`physicalHeight`, `Preview`, and every other field do not govern play here. The
reference files are large because each pixel is its own record; load them with
code rather than reading them as text.

Saved levels use the same keys as the reference files, with unused containers
left empty.

Draw row `y = 0` at the bottom. Draw the lanes below the board, the tray beside
them, and the belt around the board with its shooters and their remaining ammo.
Confirmed colors for the most common ids: `0 #18c8ef`, `1 #1c4df3`, `2 #3c4049`,
`3 #58e62f`, `4 #ff8a0f`, `5 #f55da6`, `6 #8b44e8`, `7 #d91528`, `8 #4fd4f2`,
`9 #ffc531`, `10 #f2f2f2`, `11 #7f593c`, `12 #1ca613`, `13 #0f8e6e`. Pick distinct
colors for any other id.

These rules were inferred from reference levels and play observation; they do not
certify compatibility with the full original game.
