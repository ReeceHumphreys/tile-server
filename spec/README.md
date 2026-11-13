# Tiled dataset contract

This is the whole agreement between something that publishes a tiled planetary dataset and
something that reads one. A publisher can be written in any language, and a reader need not
draw a globe. Neither should need the other's source code.

Publishers and readers prove they agree with the files in [`conformance/`](conformance),
not by sharing code. A tool keeps its own copy of those files and checks itself against it.

## The grid

The grid is named `geographic-polar`. It has four **roots**, each its own quadtree. A
position on a root is `u` and `v`, each from 0 to 1, with `u` to the right and `v` up.

| Root | Covers | Position |
| ---- | ------ | -------- |
| 0 | Western hemisphere | longitude = (u − 1) · 180°, latitude = (v − 0.5) · 180° |
| 1 | Eastern hemisphere | longitude = u · 180°, latitude = (v − 0.5) · 180° |
| 2 | North cap | see below |
| 3 | South cap | see below |

Latitude is the angle of the surface normal and longitude is positive east. On a sphere
this is ordinary planetocentric latitude. On a flattened body it is geodetic latitude.

The caps are polar stereographic squares of half-width `H = π/32`:

```
X = (2u − 1) · H          Y = (2v − 1) · H
t = tan(colatitude / 2)   colatitude is measured from the cap's own pole
north:  X = t · sin(longitude)    Y = −t · cos(longitude)
south:  X = t · sin(longitude)    Y =  t · cos(longitude)
```

On a sphere of radius R, multiply X and Y by 2R to get a true-scale `+proj=stere` plane.

A cap's square reaches 78.8° at the middle of each edge and 74.2° at its corners. A cap
promises real data only poleward of 78.8°. Outside that circle, its corners may just repeat
nearby pixels. The geographic roots hold data for all latitudes, so roots 0 and 1 alone are
a complete plate carrée pyramid: a reader that doesn't want the caps can ignore them. Where
the two overlap, the choice of root is the reader's.

### Addresses and levels

A tile address is `root/level/x/y`. At level L a root is 2^L tiles across, and `x` and `y`
run from 0 to 2^L − 1 with tile (0, 0) at u = 0, v = 0. **`y` counts up.** This differs
from XYZ map tiles.

A cap is one eighth the width of a geographic root, so a cap tile has the ground size of a
geographic tile three levels deeper. That gives two meanings of "level":

- The `level` in an address is a **root level**.
- Every `maxLevel` in a document is a **dataset level**, which is the level on the
  geographic roots.

A root's deepest level for dataset level D is D on roots 0 and 1, and max(0, D − 3) on the
caps.

## Tile payloads

Every tile is a square of `tileSize + 2 · gutter` pixels. Row 0 is the top (highest `v`),
and rows run downward. The center of pixel (col, row) is at:

```
u = (x + (col − gutter + 0.5) / tileSize) / 2^level
v = (y + 1 − (row − gutter + 0.5) / tileSize) / 2^level
```

Gutter pixels hold the surface beyond the tile's edge, so filtering across tiles needs no
neighbor. Past a pole or the edge of a root they may repeat the nearest edge pixel.

**Imagery** is 8-bit RGBA PNG in sRGB. Alpha 0 means no data. What to show there is the
reader's choice, and a coarser tile is the usual one.

**Heights** are little-endian unsigned 16-bit values, row by row, with no header. Heights
have no "no data" value.

```
meters = stored · scaleMeters + offsetMeters
```

`verticalDatum` says what heights are measured from, such as `ellipsoid`, `areoid` or
`sphere`. Readers on a flattened body need `ellipsoid`.

## Documents

Documents are UTF-8 JSON. Paths in them are relative to the document, and use `/`. Each
part of a path uses only letters, digits, `.`, `_` and `-`, and doesn't start with `.`, so a
path can't leave the dataset's directory.

Three rules apply to all of them:

- **Unknown fields are ignored.** Publishers may add fields, and readers must not fail on
  them. New meaning that an old reader would get wrong needs a new `version`.
- **Optional means absent.** `null` is not a way to leave out a field.
- **Limits are part of the contract.** A document outside them is invalid even when it
  could be read.

### Imagery manifest

A complete pyramid: every tile down to each root's deepest level exists. Tiles are at
`{root}/{level}/{x}/{y}.png`.

| Field | Required | Value |
| ----- | -------- | ----- |
| `version` | yes | `2` |
| `id` | yes | Non-empty string |
| `body` | yes | Non-empty string naming the body |
| `radiusMeters` | yes | Number above 0 |
| `projection` | yes | `"geographic-polar"` |
| `format` | yes | `"png"` |
| `colorSpace` | yes | `"srgb"` |
| `tileSize` | yes | Integer, 1 to 2048 |
| `gutter` | yes | Integer, 1 to 16 |
| `maxLevel` | yes | Integer, 0 to 20 |
| `storage` | no | See [Storage](#storage) |
| `title`, `coverage`, `quantity`, `nativeResolutionMeters`, `source` | no | Description, for people |
| `producer` | no | Whatever the publisher wants to record about itself. Readers ignore it |

### Height manifest

A sparse pyramid: only the tiles listed exist.

| Field | Required | Value |
| ----- | -------- | ----- |
| `version` | yes | `2` |
| `body`, `radiusMeters`, `projection` | yes | As for imagery |
| `format` | yes | `"uint16-le"` |
| `scaleMeters` | yes | Number above 0 |
| `offsetMeters` | yes | Number |
| `verticalDatum` | yes | String |
| `tileSize` | yes | Integer, 16 to 256 |
| `gutter` | yes | Integer, 2 to 8 |
| `maxLevel` | yes | Integer, 0 to 20 |
| `minMeters`, `maxMeters` | yes | The dataset's range: `offsetMeters` ≤ `minMeters` ≤ `maxMeters` ≤ 65535 · `scaleMeters` + `offsetMeters` |
| `sources` | yes | Array of `{id, credit, sourcePage}`, all strings |
| `tiles` | yes | Object of address to tile entry, at most 200,000 entries |
| `pageLevel`, `pages` | no | See below |
| `storage` | no | See [Storage](#storage) |

A tile entry is `{path, sha256, min, max, lower, upper}`:

- `path` ends in `.u16`, and `sha256` is 64 lowercase hex digits of the decoded payload.
- `min` and `max` are the tile's exact range in meters, gutters included.
- `lower` and `upper` bound it and every tile below it. Each stays inside its parent's
  bounds and inside `minMeters` and `maxMeters`, and `lower` ≤ `min` ≤ `max` ≤ `upper`.

Every root has a `root/0/0/0` entry, and every listed tile has a listed parent. An address
may not go deeper than its root's deepest level.

Fine levels may move out of `tiles` into pages. `pageLevel` is a root level from 0 to 6, and
`pages` maps up to 8192 addresses at that level to `{path, sha256, lower, upper}`, with a
path ending in `.json`. The two fields come together. Each address must be in `tiles`, and
`lower` and `upper` stay inside that tile's bounds. A page is a JSON file `{"tiles": {...}}`
of at most 20,000 entries, all below the page's address. The limit on `tiles` doesn't count
them.

### Regional catalog

Optional detail over one imagery manifest, called its base. A catalog is usually found at
`catalog.json`, but its location is the application's choice.

| Field | Value |
| ----- | ----- |
| `version` | `1` |
| `body`, `radiusMeters`, `tileSize`, `gutter` | Equal to the base's |
| `baseId`, `baseMaxLevel` | The base's `id` and `maxLevel` |
| `sites` | Array of at most 64 sites |

| Site field | Required | Value |
| ---------- | -------- | ----- |
| `id` | yes | Unique, matching `^[a-z0-9-]+$` |
| `name` | yes | String |
| `latitudeDeg`, `longitudeDeg` | yes | Degrees, in range |
| `radiusMeters` | yes | Number above 0 |
| `maxLevel` | yes | Integer, above the base's and at most 17 |
| `source` | yes | `{sourcePage, credit, nativeResolutionMeters}`, the last above 0 |
| `tiles` | yes | Array of at most 20,000 addresses |
| `storage` | no | See [Storage](#storage), relative to the site's directory |

Site tiles are at `{site id}/{root}/{level}/{x}/{y}.png`. Each address is deeper than the
base on its root and no deeper than the site on its root. No address appears twice in a
catalog. A site tile deeper than the base's next level on that root has its parent in the
same site.

A site tile is a finished image, already blended over the base.

## Storage

Without `storage`, each tile is its own file at its path.

With it, tiles live in [PMTiles v3](https://github.com/protomaps/PMTiles) archives:

```json
{
  "storage": {
    "type": "pmtiles",
    "profile": "geographic-polar-v1",
    "archives": { "0": "pmtiles/0.pmtiles", "1": "pmtiles/1.pmtiles" }
  }
}
```

An archive holds one root. `archives` has up to 4096 entries, each mapping a key to a path
ending in `.pmtiles`. The key is the tile's path with its last three parts removed, so it
ends in the root: `0` for imagery tile `0/3/1/2.png`, and `global/0` for height tile
`global/0/3/1/2.u16`.

Inside an archive, Z is `level`, X is `x` and Y is `y`, **with no flip**. The header's
minimum and maximum zoom must be true, because readers use them to find tiles. Its
geographic bounds mean nothing, so use this contract's grid. Height payloads may be gzip
compressed, as PMTiles allows. PNG payloads are not.

Reading needs only HTTP GET with `Range` on any static host. There is no server API, so any
server that hosts files can serve a dataset. Browser readers also need CORS.

## Changes

Adding an optional field is not a change to `version`. Anything else that would make an
existing reader or publisher wrong is a new `version` for that document, or a new `projection`
or `profile` if the grid or storage changes. The conformance files change in the same commit.

## Conformance

[`conformance/grid.json`](conformance/grid.json) has reference values:

- `points`: `root`, `u`, `v` and the `latitude` and `longitude` in degrees. Longitude means
  nothing at a pole, and at ±180° either sign is right.
- `levels`: `root` and dataset `maxLevel`, with that root's `rootMaxLevel`.
- `pixels`: a pixel of a tile, with the `u` and `v` of its center.
- `heights`: a stored value and encoding, with its `meters`.

Agree to within 1e-9.

[`conformance/documents.json`](conformance/documents.json) is a list of cases:
`{name, type, base?, document, valid}`. `type` is `imagery`, `heights` or `regions`, and
`base` is the imagery manifest a catalog is checked against. A publisher must not publish
what a case marks invalid. A reader must reject it.

Both files come from [`conformance/generate.py`](conformance/generate.py), which uses only
the formulas above and the Python standard library. To change them, edit it and run it.
