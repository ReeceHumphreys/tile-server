# Preparing lunar data

The viewer ships with the 2K overview image. WAC tiles and the LOLA elevation grid are generated locally and gitignored.

## Tools

Use Python 3.12+, uv, and curl. The offline tool is the installable `tile_service` package. Rasterio supplies GDAL for projection, resampling, TIFF caching, and PNG encoding. Dependencies are declared in `pyproject.toml` and locked in `uv.lock`.

```sh
uv sync --locked
```

The globes viewer does not use Python or GDAL. It reads these manifests over HTTP. Height bounds stay in globes at `native/elevation-bounds.wasm`. Finished tiles stay valid. A complete pipeline-version-2 pyramid is kept as-is; any other resumed build must match the current source and settings. See the [tile guide](tile-generation.md) for Mars and Earth.

Every dataset is a build target in `datasets/catalog.json`, named after its
output under `public/data`. `uv run tile-service list` shows them and their
state. `build TARGET` downloads and verifies the target's sources, then builds
it; rerunning resumes an interrupted build or just verifies a finished one.
`--dry-run` prints sources, sizes and outputs without downloading. Dependency
installation and viewer startup never download source data.

## Imagery

The source is the [USGS LROC WAC global 100 m morphology mosaic](https://astrogeology.usgs.gov/search/map/moon_lro_lroc_wac_global_morphology_mosaic_100m), about 6 GB.

```sh
uv run tile-service build moon/wac --dry-run
uv run tile-service build moon/wac --max-level 2 --output public/data/moon/wac-preview
uv run tile-service build moon/wac
```

Downloads resume from a `.part` file, check size and the TIFF signature, and store a local SHA-256 receipt. That receipt detects local changes. It is not a publisher checksum.

The build writes `public/data/moon/wac/` unless `--output` is set. One cached reprojection per cube face supplies the lower levels. Builds resume after interruption and write `manifest.json` only after every tile exists. Changing `--jobs` is resumable. Changing source, tile size, gutter, or level is not: use a new output directory.

| Option        | Meaning                                                  |
| ------------- | -------------------------------------------------------- |
| `--jobs`      | Face workers, 1–6. Default 2                             |
| `--max-level` | Deepest quadtree level, 0–12                             |
| `--output`    | Another output directory, e.g. for a preview             |
| `--source`    | A local raster instead of the catalog download           |

Tile size (256) and gutter (2) come from the dataset file. Library callers can
set them, PNG compression and face-cache retention on `tile_service.imagery.build`.

The viewer loads `/data/moon/wac/manifest.json` by default. Another pyramid:

```text
http://localhost:5173/?imagery=data/moon/wac-preview/manifest.json
```

Visible tiles are requested on demand, four at a time. Ready ancestors stay on screen while children load, then fade in. The GPU keeps a fixed resident set. It does not download the whole pyramid.

## Elevation

```sh
uv run tile-service build moon/elevation
```

This downloads the NASA global LOLA grid (about 32 MB), checks its pinned SHA-256, and writes `public/data/moon/elevation/`. Samples are about 1.9 km apart. This is global relief, not meter-scale terrain.

NASA stores unsigned half-meters above a 1,727,400 m sphere. The viewer converts that to height above its 1,737,400 m sphere:

```text
heightMeters = uint16Sample * 0.5 - 10000
```

Rows run north to south. Longitude spans −180° to +180°, pixel centers. The prepared range is −8,981.5 m to +10,685.5 m. Polar end rings are replaced by their longitude means so every longitude agrees at the pole. The original TIFF is kept. Do not use this derivative for measurements of those rings.

The globe renders first. Elevation then loads in the background. The same global grid is sampled for every tile. Camera altitude is height above that field, with 10 m of clearance. That is radial clearance, not a collision mesh.

Bounds for culling are built with `native/elevation-bounds.wasm`. If that module fails to load, `src/elevation/bounds.ts` builds the same arrays in TypeScript.

The Wasm bounds kernel belongs to the globes renderer; this service does not build it.

## Checks

```sh
uv run pytest
```

Tests use analytic rasters and local transfer fixtures. See [deployment](deployment.md) for checks against the actual serving container.

## Apollo landing sites

The optional regional layer uses LROC NAC orthophotos published with the landing-site
DTMs. These are georeferenced images, not the raw camera frames or the shaded-relief
products. The catalog in `datasets/apollo-nac.json` pins the official download URLs,
byte lengths and ETags checked on 2026-10-17. Coordinates come from the same
`examples/moon/data/landing-sites.ts` table used for the globe's labels and site selector.

```sh
uv run tile-service build moon/apollo --dry-run
uv run tile-service build moon/apollo --site apollo11
uv run tile-service build moon/apollo
```

`--dry-run` prints the plan without downloading. `--site` (repeatable) limits the
build; without it, all six sources download, about 6.30 GB (5.87 GiB), plus
generated tiles. Files resume and existing sources are
checked against their local SHA-256 receipts. The ETag guards resumed downloads;
these receipts are locally computed, not publisher-provided SHA-256 checksums.

| Site      | Orthophoto source                                                                                         | Native sampling | Source size |
| --------- | --------------------------------------------------------------------------------------------------------- | --------------- | ----------- |
| Apollo 11 | [M150361817](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO11_M150361817_50CM)         | 0.5 m/pixel     | 450.4 MiB   |
| Apollo 12 | [M120005333](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO12_M120005333_50CM)         | 0.5 m/pixel     | 51.5 MiB    |
| Apollo 14 | [M150633128](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO14_M150633128_50CM)         | 0.5 m/pixel     | 474.0 MiB   |
| Apollo 15 | [M111571816](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO15_M111571816_50CM)         | 0.5 m/pixel     | 558.3 MiB   |
| Apollo 16 | [Site mosaic](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO16_MOSAIC_50CM)            | 0.5 m/pixel     | 2,560.4 MiB |
| Apollo 17 | [Taurus–Littrow mosaic](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO17_MOSAIC_120CM) | 1.2 m/pixel     | 1,917.7 MiB |

The default region is a 2 km radius around each landing point. The data tool
reprojects only its sparse cube tiles, starting one level below the existing WAC
pyramid's finest tiles. Default maximum is level 15 for the 0.5 m sources and 14
for Apollo 17; tile sampling varies over the cube sphere and is separate from
source resolution. Extra terrain subdivision cannot create additional photo detail.
The full source TIFF is downloaded once; this is not a remote cropped-TIFF service.

Options: `--radius-meters 100..20000`, `--max-level 1..16`, `--output /path/apollo`. Generation rejects more than 20,000 regional tiles or a
region crossing a cube-face edge. All current Apollo sites fit within one face.
Use the normal level-7 WAC base. Another radius/depth/source requires a new output
directory, avoiding stale tiles; rerunning identical settings resumes completed PNGs.
Run site builds sequentially; one `build moon/apollo` handles that for you.

Regions go into `public/data/moon/apollo/<site>/`. A catalog is atomically published
only after that site's tiles finish. The viewer discovers `../apollo/catalog.json`
relative to its WAC manifest, in the background after the initial globe render.
For a custom output location, set `?regions=data/moon/my-regions/catalog.json`.
Regional data is gitignored and must be published alongside the global pyramid.

NAC photographs are blended onto resampled WAC offline, with a soft 100 m maximum
border. Missing NAC coverage retains WAC, rather than introducing black rectangles.
This avoids needing another texture array or vertex buffer. Each site is scaled by
one gain so its average matches the WAC underneath. Local contrast is unchanged.
Only addresses declared in the catalog are requested. They share the existing
four-request scheduler, 128-image residency limit, ancestor fallback and fades.
The browser does not download every regional tile at startup.

After generation, reload and choose **Apollo landing site → Apollo 12** (or another
prepared site), then **Inspect altitude → Surface**. At the initial Sun longitude,
Apollo 12 is on the night side: set Sun longitude near −23° to illuminate it.
The site selector starts at 10 km to give context before zooming in.

Apollo 12 was downloaded and generated during implementation: 3,604 PNG tiles,
about 84 MiB on disk, around the 2 km region. The center tile visibly contains the
lander and its shadow. Other source endpoints/sizes were checked, but those larger
sources were not downloaded. Tests use analytic rasters for border composition,
sparse addressing, resume, metadata validation and runtime selection. Elevation
remains the existing ~1.9 km LOLA grid; these are higher-resolution images only.

## Earth sites

Starbase and Kennedy Space Center use the same regional catalog as the Apollo
photographs: extra cube tiles above the global pyramid, same `{face}/{level}/{x}/{y}.png`
addresses, no second imagery cache. The base is the level-7 s2cloudless-2020 pyramid.
Each site is a 2 km disk at levels 8–16 (about 0.8 m per pixel at the finest level).

The photographs are USDA NAIP 60 cm quads, public domain, read as windows from the
Microsoft Planetary Computer COGs pinned in `datasets/earth-sites.json` (checked
2026-10-18). Starbase is the orbital launch site, not the production buildings about
3 km west. Kennedy Space Center is LC-39A; the Vehicle Assembly Building and LC-39B
are outside the disk. Generated tiles are blended onto the cloudless base, which is
CC BY-NC-SA 4.0, so those PNGs follow that license even though NAIP itself does not.

```sh
uv run tile-service build earth/sites --dry-run
uv run tile-service build earth/sites --site starbase
uv run tile-service build earth/sites
```

Output is `public/data/earth/sites/catalog.json`. The Earth viewer loads it after the
globe is up. A missing catalog is ignored. `?regions=` replaces it. Rebuild a site
into a new `--output` directory if the radius, depth, or source changes.

## Streamed heights

The [elevation tile guide](elevation-tiles.md) describes the bounded numeric cache,
coarse LOLA pyramid, paged min/max metadata and regional SLDEM/NAC import.

```sh
uv run tile-service build moon/elevation-tiles
uv run tile-service build moon/apollo-elevation --site apollo12-dtm
```

When the tiled manifest exists, the viewer uses it in place of downloading the
full global height blob. The old global grid and its Wasm bounds builder are
retained as a compatibility fallback. Apollo 12 terrain is already prepared locally.

### Inspecting the poles

Turn off **Terrain sunlight shading** to inspect the photographed imagery without
our additional terrain lighting. The WAC mosaic already contains shadows and
mosaic boundaries; disabling this control cannot reveal permanently shadowed
terrain. The overview shader uses `atan2(y, length(xz))` for latitude to retain
precision near either pole.

The [USGS WAC metadata](https://astrogeology.usgs.gov/search/map/moon_lro_lroc_wac_global_morphology_mosaic_100m)
documents polar/equatorial brightness discontinuities. For Shackleton detail,
the [controlled south-pole NAC average mosaic](https://data.lroc.im-ldi.com/lroc/view_rdr/NAC_POLE_SOUTH_CM_AVG)
provides 1 m/pixel polar stereographic tiles, and the
[LOLA south-pole DEM mosaic](https://pgda.gsfc.nasa.gov/products/81) provides
5 m/pixel heights in the DE421 Mean Earth frame. These are candidate upgrades,
not data currently installed by the Apollo download script. Permanently
shadowed interiors need a separate PSR imagery product; ordinary NAC images
will still show darkness there.

### Download the Shackleton polar pack

```sh
uv run tile-service build moon/south-pole-imagery moon/south-pole-elevation --dry-run
uv run tile-service build moon/south-pole-imagery moon/south-pole-elevation
```

The imagery and elevation layers are separate targets; build either alone.

This builds **44 km radius coverage around the south pole**, with finer tiles
within **16 km of Shackleton**. Both tiers use the same NAC source and brightness
adjustment, so crossing the inner detail boundary does not switch back to WAC.
The outer coverage blends into WAC over 4 km. This covers roughly 88.55–90° S,
rather than the entire polar cap. Raw sources cover more territory:
four central controlled NAC average-mosaic quadrants (1 m/pixel, 88.5–90° S)
and the 87–90° S LOLA DEM (5 m/pixel). Verified source sizes total **11.74 GB
(10.94 GiB)**, plus generated imagery and height tiles. Files stay under
`data/raw/moon/polar`; the tool joins the NAC quadrants through a small GDAL
VRT, without allocating a second giant merged TIFF. Existing Apollo regions
remain in the regional catalog.

Default output goes into the viewer's existing imagery catalog
`public/data/moon/apollo/catalog.json` and height pyramid
`public/data/moon/elevation-tiles/manifest.json`. Reload, choose **Go to location →
Shackleton crater**, then zoom in. Generate WAC and the coarse height pyramid
first, as described above. All four NAC quadrants are required because the
Shackleton region crosses the geographic pole. Their individual source hashes
are preserved in the region's provenance.

Default context tiles stop at cube level 11, about 6.63 m/pixel for imagery at
the pole. Shackleton detail stops at level 13, about 1.66 m/pixel; the source
remains 1 m/pixel. Height tiles use the existing numeric encoding and the same
two coverage tiers. Override `--radius-meters` or `--max-level` per target to
choose another cost/detail balance. Oversized requests are rejected before
download; native builds retain the 20,000-tile safety limit. Changing settings
requires a fresh output location, as for Apollo packs. The overview ring and
feather width live in `datasets/south-pole-nac.json` (imagery) and
`datasets/south-pole-lola.json` (elevation): ordinary sites files whose single
site carries a `mosaic` or `download` source.
The runtime retains WAC/LOLA fallback and bounded streaming caches. Cache
refinement preserves baseline parent coverage across the view before spending
remaining slots on deeper detail. Source brightness differences can still
remain at the outer boundary.

Rerunning the build reuses verified source downloads and completed tiles.
The broader imagery replaces the old Shackleton catalog entry only after its
generation completes; old files remain on disk. Elevation publishes separately
after its build completes. Reload the viewer after both stages finish.

Sources: [LROC controlled NAC average mosaic](https://data.lroc.im-ldi.com/lroc/view_rdr/NAC_POLE_SOUTH_CM_AVG),
Archinal et al. (2023); [PGDA south-pole LOLA DEM](https://pgda.gsfc.nasa.gov/products/81),
Barker et al. (2021). The DEM publisher supplies a weak ETag; its pinned
Last-Modified date is sent as `If-Unmodified-Since` instead of the invalid weak
`If-Match`. Completed downloads have local SHA-256 receipts. Sizes and headers
were checked on 2026-10-17. Synthetic polar quadrants also exercise coverage and
catalog replacement in the integration tests.
