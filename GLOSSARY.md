# Glossary

**Target**: a named thing `tile-service build` makes, called after its output under
the build root (`moon/wac`, `earth/sites`). Declared in `datasets/catalog.json` as
a layer plus its settings. _Avoid_: job, command, dataset (for the output).

**Root**: one of the grid's four quadtrees: 0 and 1 are the western and eastern
hemispheres in latitude and longitude, 2 and 3 the north and south **caps** in polar
stereographic projection. A tile address is `root/level/x/y`, y up. A cap tile matches the
ground size of a geographic tile three levels deeper. The contract defines them, and
`tile_service.roots` implements them.

**Layer**: what a target builds: `imagery` (global PNG pyramid), `grid`
(global equirectangular DEM), `heights` (global uint16 height pyramid),
`imagery-regions` or `height-regions` (detail disks over a base).

**Source**: how a raster is obtained: `download`, `mosaic`, `wmts`, `naip`, `polygons`, or a
local file. Every source yields a verified local raster plus its provenance.
_Avoid_: fetcher, input.

**Dataset**: a `datasets/*.json` record pinning one source raster: URL, size, ETag,
checksum, body radius and credit.

**Site**: one regional disk in a sites file: center, radius, optional overview disk
and feather, and optionally its own source.

**Base**: the target a regional layer refines; its pixels are part of the region's
identity.

**Identity**: what determines a build's output bytes (source hash, radius,
geometry, levels, encoding, toolchain), recorded in `build-state.json`. A
directory only resumes a build with the same identity. _Avoid_: settings, config.

**Provenance**: descriptive metadata published with a build (title, credit, source
page, product, components). Not identity: changing it republishes manifests
without rebuilding tiles.

**Height index**: the on-disk height pyramid format: `manifest.json`, paged tile
metadata under `metadata/`, and uint16 tiles.

**Document**: a published JSON file: an imagery manifest, a regional catalog (over one
imagery manifest), or a height manifest. `tile_service.documents` builds and validates them
against the contract.

**Contract**: `spec/README.md` and its conformance files, the only agreement between this
tool and whatever reads its output. _Avoid_: the globe's rules.

**Dataset level**: the `maxLevel` in a document, which is the level on the geographic roots.
**Root level**: the `level` in a tile address. A cap's root levels run three behind.
`root_max_level` converts one way; nothing else does.

**Tile store**: where a dataset's tiles are read from: its loose files, or once
packed, its PMTiles archives. Loose tiles may be deleted after packing.

**Release**: a self-contained directory of packed documents and PMTiles archives,
ready to serve or upload.
