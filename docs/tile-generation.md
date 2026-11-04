# Generate tiles for another body

The renderer accepts static files over HTTP. The offline Python tool uses GDAL,
through rasterio, for reprojection, SHA-256 receipts, and resumable tile
generation. Imagery and elevation may live on the same server as the app or a CDN
with CORS enabled. Serve HTTPS and retain parent tiles at all levels; do not
upload just the finest leaves. Immutable dataset versions are preferable when
caching files. Binary height tiles must not be served as an HTML fallback.

## Data tool prerequisites

Use Python 3.12+, uv, and curl, or the Docker `tools` target. Run `uv sync --locked` to install the project and its locked dependencies in `.venv`. Rasterio bundles GDAL; no separate GDAL compiler is required. Run commands with `uv run tile-service ...`. See [deployment](deployment.md) for container usage.

## Imagery

Start with a georeferenced **uint8, one-band or RGB GeoTIFF** on the same sphere
as the runtime body. Supply the appropriate planetary CRS; merely renaming an
Earth CRS does not transform its datum. The tool accepts projected source rasters
but checks that their reference sphere matches the requested radius. Ellipsoidal
source data needs explicit conversion to the chosen spherical reference first.
Stretch higher-bit-depth scientific imagery to a display product separately;
keep the calibrated originals and provenance. This step is not an albedo fit.

Describe the source in a dataset file in your workspace, e.g.
`datasets/mars-imagery.json` (`sourceFile` is relative to `data/raw`; the
radius here is an example spherical Mars model):

```json
{
  "id": "mars-surface-v1",
  "body": "Mars",
  "title": "My Mars mosaic",
  "radiusMeters": 3389500,
  "sourceFile": "mars/mars-surface.tif",
  "sourceUrl": "https://example.org/original-dataset",
  "sourcePage": "https://example.org/dataset-documentation",
  "credit": "Dataset author and required attribution",
  "nativeResolutionMeters": 100,
  "coverage": { "west": -180, "east": 180, "south": -90, "north": 90 },
  "quantity": "display imagery; may retain original lighting",
  "tileSize": 256,
  "gutter": 2,
  "maxLevel": 2
}
```

Then name a build target for it in the workspace's `datasets/catalog.json`
(its targets are added to the packaged ones):

```json
"mars/imagery": {
  "layer": "imagery",
  "source": { "type": "download", "dataset": "mars-imagery.json" },
  "description": "My Mars mosaic"
}
```

```sh
uv run tile-service build mars/imagery --source /absolute/path/mars-surface.tif --max-level 2 --output public/data/mars/imagery-check
uv run tile-service build mars/imagery --source /absolute/path/mars-surface.tif --jobs 4
```

Add `sourceUrl`/`expectedBytes`/`sourceETag` to the dataset and drop `--source`
to have the build download the source itself. Regional imagery is another
target with `"layer": "imagery-regions"`, a `base` target and a `sites` file;
see `datasets/apollo-nac.json` for the site shape.

Start with max level 2 to check all faces, longitude seam, poles, known landmarks,
and orientation. Increase depth only after checking the reprojection. The global
builder currently supports levels 0–12, jobs 1–6, core power-of-two sizes 16–2048,
and gutters 1–16. Tile counts grow as `6 * (4^(L+1) - 1) / 3`; plan storage before
full generation. Re-running the same command resumes completed work; changed
sources/settings require a fresh output. Cached reprojection avoids repeating
full GDAL warps for every level. Regional imagery uses a `regions` target with a
sites file and a base target; see the Moon [data guide](data.md).

## Viking MDIM 2.1 color

`datasets/mars-viking-mdim21-232m.json` is the USGS colorized Viking mosaic: public-domain uint8 RGB, 256 pixels per degree, on the 3,396,190 m sphere stored in the GeoTIFF. The download resumes from a `.part` file and checks that pinned size, the S3 ETag, and the TIFF signature.

```sh
uv run tile-service build mars/viking --dry-run
uv run tile-service build mars/viking --max-level 2 --output public/data/mars/viking-preview
uv run tile-service build mars/viking
```

The download is 12,742,411,029 bytes and lands at `data/raw/mars/mars-viking-mdim21-232m.tif`. The source is about 232 m/pixel. The level-2 command is a seam and pole check. The last command writes the level-7 pyramid the Mars example loads: 131,070 tiles, about 207 m per pixel at a face center, the same depth as the Moon WAC pyramid. Open the globe with `{ name: "Mars", radiusMeters: 3396190 }`. The tool accepts this file only at that radius. Illumination is already in the mosaic. Credit NASA Ames and the USGS Astrogeology Science Center.

## EOxCloudless 2020

`s2cloudless-2020` is CC BY-NC-SA 4.0. A cube pyramid made from it stays under
that license. Attribution, stored on the manifest, is: EOxCloudless
https://cloudless.eox.at by EOX IT Services GmbH (Contains modified Copernicus
Sentinel data 2020). The license page allows stitched WMTS requests up to 4096 px
each. The command downloads the WGS84 geographic matrix and writes one
equirectangular GeoTIFF on the 6,378,137 m sphere. Published longitude and
latitude stay the sphere's angles. Zoom 7 is the default: 32,768 tiles, about
611 m per pixel, and cube levels 0–7. That is the same pyramid depth as the Moon
WAC mosaic (131,070 tiles). On Earth the last cube level is about 390 m per
pixel, so it resamples this mosaic. Zoom 0–7 is accepted. A zoom-5 mosaic
already at `data/raw/earth/s2cloudless-2020.tif` is left as it is.

```sh
uv run tile-service build earth/s2cloudless-2020-z7 --dry-run
uv run tile-service build earth/s2cloudless-2020-z7
```

The target's `zoom` lives in `datasets/catalog.json`; another zoom is another
target with its own `sourceFile`. Check faces with `--max-level 2 --output …`
before the full build. The download uses 16 fetchers. One failed tile does not
stop the others, and a rerun skips tiles already saved.

Open the manifest with body `{ name: "Earth", radiusMeters: 6378137 }`. Turn
terrain shading off. Daylight is already in the photograph. Keep the credit
visible next to the globe.

## Global elevation

Use a single-band georeferenced DEM on the same sphere, with heights relative to
its radius. Convert datum differences before this step. Explicitly supply unit
scale/offset when source values are not meters. The current encoding cannot
represent heights outside −10000 to +22767.5 m; the tool rejects them.

The Earth example uses ETOPO 2022 ice surface at 60 arc-seconds (465,969,062 bytes, DOI 10.25921/fd45-gt74). The source is WGS 84 longitude and latitude with EGM2008 orthometric meters. Those angles are placed on the 6,378,137 m sphere and the meters become radial offsets. Deep trenches are clamped to −10000 m so they fit the shared uint16 encoding; the lunar encoder still rejects out-of-range samples. The prepared grid is 16,384 by 8,192, about 2.4 km per pixel, the finest grid the tool accepts. The browser global-grid path stops at 8,192 pixels, so the viewer loads the tiles and not that grid. `--dry-run` prints the download and writes nothing.

```sh
uv run tile-service build earth/elevation earth/elevation-tiles
```

The Mars example uses the MGS MOLA 463 m DEM. Heights are meters above the areoid on the same 3,396,190 m sphere as the Viking mosaic. The prepared grid is 11,520 by 5,760, about 1.85 km per pixel, which matches the Moon LOLA grid's 1.90 km spacing. Level 5 tiles are about 1.66 km at a face center, the same relationship as the Moon's level-4 LOLA pyramid. The browser global-grid path still stops at 8,192 pixels, so the viewer loads these tiles and not the prepared grid.

```sh
uv run tile-service build mars/elevation mars/elevation-tiles
```

A `grid` target takes its width and height from `datasets/catalog.json` and the
body, radius, `inputScale`/`inputOffset` and credit from its dataset file.
Preparation writes a north-to-south, east-positive global numeric grid into a
new or empty directory; rebuilding an up-to-date grid only verifies it. Missing
cells become 0 m; this fill is a deliberate approximation, not measured terrain. Choose an
appropriate baseline and document it. Numeric tile generation then resumes when
rerun; it preserves min/max bounds rather than averaging away peaks. The global
height builder supports levels 0–5. Higher detail belongs in sparse regions.

```python
from tile_service import heights

heights.add_region(
    "public/data/mars/elevation-tiles",
    "./local-dem.tif",
    "local-dtm",
    -4.5,
    137.4,
    2000,
    max_level=13,
    credit="DTM author",
    source_page="https://example.org/dtm",
)
```

Regional heights inherit body/radius from the base manifest and require matching
source CRS. Detail caps are currently restricted to one cube face; high-detail
radius is 100–20000 m. Optional `overview` and `feather` arguments add a larger
coarse context cap. Sources worth keeping belong in a `height-regions` target. The base must be generated before regions. Runtime requests use
parent fallback, and separately bounded imagery/elevation residency.

## Output contract

Imagery addresses are `{face}/{level}/{x}/{y}.png`; each image includes gutters.
The manifest records body, radius, axes, row order, UV transform, source hash and
level range. Face order is +X, −X, +Y, −Y, +Z, −Z. Address Y increases upward in
face space; image rows run top to bottom. Use the manifest conventions rather
than assuming XYZ/Web Mercator coordinates.

Height manifests describe little-endian uint16 tiles and conservative min/max
metadata, optionally split into checksummed pages. Runtime verifies tile/page
checksums. All six roots are required, with complete ancestor chains. Do not
hand-edit generated bounds or hashes. See exported `ImageryManifest`,
`RegionalCatalog`, and `HeightTileManifest` types for the exact schema.

Configure `createGlobe` with the generated manifest URLs and the **same body name
and radius**. Keep dataset credits accessible in your application's UI. The Moon
example supplies its own credits, Apollo hardware, markers and star background;
none of those are required by the library.
