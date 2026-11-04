# Glossary

**Target**: a named thing `tile-service build` makes, called after its output under
the build root (`moon/wac`, `earth/sites`). Declared in `datasets/catalog.json` as
a layer plus its settings. _Avoid_: job, command, dataset (for the output).

**Layer**: what a target builds: `imagery` (global PNG cube pyramid), `grid`
(global equirectangular DEM), `heights` (global uint16 height pyramid),
`imagery-regions` or `height-regions` (detail disks over a base).

**Source**: how a raster is obtained: `download`, `mosaic`, `wmts`, `naip`, or a
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

**Document**: a published JSON file the globe reads: an imagery manifest, a
regional catalog (over one imagery manifest), or a height manifest.
`tile_service.documents` builds and validates them with the globe's rules.

**Tile store**: where a dataset's tiles are read from: its loose files, or once
packed, its PMTiles archives. Loose tiles may be deleted after packing.

**Release**: a self-contained directory of packed documents and PMTiles archives,
ready to serve or upload.
