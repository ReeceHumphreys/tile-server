# Streamed lunar elevation

The globe prefers `data/moon/elevation-tiles/manifest.json`. When it exists, the
viewer does not fetch the old 32 MiB global height blob or build its full browser
bounds pyramid. A small fixed grid is still drawn once per selected terrain tile.
Height data comes from a bounded numeric array texture, independently of imagery.
If the tiled manifest is missing, the legacy LOLA/Wasm path remains available.

## Prepare the coarse globe

```sh
uv run tile-service elevation
uv run tile-service height-tiles
```

The second command converts the prepared LOLA grid into cube-face uint16 tiles.
Default core size is 128, gutter is 2, and the complete global pyramid ends at
level 4: 2,046 files, approximately 68 MiB of uncompressed samples on disk.
This preserves the source's broad relief rather than inventing higher resolution.
`--tile-size 16..256`, `--gutter 2..8`, `--max-level 0..5`, `--source` and `--output`
can change this baseline. An output directory belongs to one set of settings.
Atomic files make interrupted builds resumable. Rebuilding an already populated
base preserves published regional refinements.

The native tool reads the 32 MiB source once offline. That source is not kept in
the browser when tiled mode is active. Source hashes, output ranges and tile
checksums are recorded. Pixel rows are north-to-south, face addressing is bottom
left, and values decode as `uint16 * 0.5 - 10000` meters above the 1,737,400 m
reference sphere. The quantization step is 0.5 m; it is not centimeter terrain.

## Add Apollo terrain

```sh
uv run tile-service apollo --elevation --list
uv run tile-service apollo --elevation --site apollo12 --info
uv run tile-service apollo --elevation --site apollo12
uv run tile-service apollo --elevation --site all
```

These download numeric DTMs, separately from `data:apollo`'s photographs. The
[LROC Apollo 12 DTM](https://data.lroc.im-ldi.com/lroc/view_rdr_product/NAC_DTM_APOLLO12)
is a 2 m/pixel, 32-bit GeoTIFF, about 12.9 MiB. Apollo 11–16 DTMs have 2 m sampling;
Apollo 17's mosaic has 5 m sampling. `datasets/apollo-elevation.json` contains the
six verified URLs, lengths and ETags. Downloads resume and write integrity receipts.
The default processing radius is 2 km; `--radius-meters` and `--max-level` override it.
Other DTMs are opt-in and were not downloaded during implementation.

Apollo 12 is already generated locally: 967 finer tiles through level 14, plus one
metadata page. The published coarse index is about 464 KiB; the fine metadata page
is about 241 KiB. Total global-plus-Apollo-12 dataset storage is about 107 MiB.
It is discovered after reload. Select Apollo 12, then the surface altitude preset.
Both fine photographs and fine height geometry can now load for that location.

## Import SLDEM or another local numeric raster

`height-region` reads GDAL raster windows and writes only a bounded patch. It does
not allocate the full source raster in the browser. The input needs a lunar CRS
with a 1,737,400 m sphere, one numeric band, and correct nodata metadata.
The explicit input scale/offset convert raw samples to meters above that sphere;
the tool does not infer vertical units from a filename or silently clip invalid
heights. Heights outside the common encoding range are rejected.

For an SLDEM2015 JP2 covering Apollo 15, an example is:

```sh
uv run tile-service height-region \
  --source /path/SLDEM2015_512_00N_30N_000_045.JP2 \
  --id sldem-apollo15 \
  --latitude 26.13239 --longitude 3.63330 \
  --radius-meters 20000 --max-level 9 \
  --input-scale 0.5 --input-offset 0 \
  --credit 'NASA GSFC/MIT/JAXA SLDEM2015' \
  --source-page https://pgda.gsfc.nasa.gov/products/54
```

The official JP2's [auxiliary metadata](https://imbrium.mit.edu/EXTRAS/SLDEM2015/TILES/JP2/SLDEM2015_512_00N_30N_000_045_AUX.XML)
specifies a 0.5 scale and -32768 nodata. Its
[scientific label](https://imbrium.mit.edu/EXTRAS/SLDEM2015/TILES/JP2/SLDEM2015_512_00N_30N_000_045_JP2.LBL)
uses the DE421 mean-Earth/polar-axis frame and 1737.4 km reference radius. Preserve
georeferencing/nodata when obtaining or converting this data. JP2 input needs
OpenJPEG in the GDAL build; the pinned rasterio wheel was not checked against an
SLDEM JP2. Other SLDEM encodings may use different units;
inspect their own metadata rather than reusing this scale automatically.
[SLDEM2015](https://pgda.gsfc.nasa.gov/products/54) covers ±60° latitude; LOLA remains
the polar/global fallback. Its 512 samples/degree correspond to about 59.2 m at
the equator. This implementation did not download or measure an SLDEM source;
local GeoTIFF import is tested with analytic rasters and the real NAC model.

Generate coarser regional data before overlapping finer NAC patches. Each region
blends onto a frozen snapshot of the existing base. Updating that lower layer
later does not rewrite an already baked higher layer. A different source/radius/
scale/depth needs a new source ID/output; rerunning identical settings resumes it.
For now the generator accepts regions of 100–20,000 m radius, within one cube
face and at most 20,000 output tiles per job. It is a regional importer, not a
command to build the full global SLDEM archive. Broader coverage can be prepared
as separate regions; full-archive batch generation and parallelism remain future work.

## Runtime and bounds

- Four elevation requests, including metadata pages, run concurrently. Parents
  are admitted before children. Camera-footprint data is also requested when
  looking toward the horizon.
- The cache holds 64 height tiles plus a zero-meter fallback slot. At 132×132
  RG8 samples this is about 2.2 MiB on the GPU, plus CPU samples and upload bytes.
- Detailed metadata is paged under coarse cube addresses. At most 16 pages are
  resident; the application does not load a whole finest-resolution catalog.
  Page checksums, paths, ancestor chains and ranges are validated before use.
- Finer heights fade from the ready parent over 300 ms. CPU camera clearance,
  label placement and GPU displacement use the same encoded heights, UV
  registration, selected geometry level and blend weight. Clearance follows
  the changing height field during a fade. This remains radial clearance from
  the sampled surface, not triangle collision detection or a walking controller.
- Culling/LOD consult offline descendant ranges before fine data arrives and
  include the currently drawn parent range during transitions. Loaded-grid
  derivative bounds tighten deep subtiles, rather than forcing a full source
  cell's elevation range onto a meter-scale patch. Query memoization is bounded
  and invalidated as residency, metadata or transitions change.
- Numeric bilinear filtering decodes packed bytes without sRGB conversion or
  mipmaps. Two-pixel overscan supplies neighboring height samples. Per-tile skirts
  cover remaining geometry/LOD cracks; this is not explicit edge stitching.
- Relief normals sample the same tiled height blend. Local UV transforms avoid
  multiplying fine face coordinates into large texture offsets. Normal tangents
  are differentiated on the sphere rather than subtracting planet-sized positions.
- Image and height bindings share one interleaved instance buffer. The geometry
  still uses eight vertex-buffer bindings, now with sixteen vertex attributes.

The flat reference sphere is shown until root heights arrive; ready parents
remain available if a child fails. Corrupt or dimension/range-mismatched payloads
are rejected. The UI reports unavailable height data. `?heights=.../manifest.json`
selects another height catalog. Publish the generated height directory with the
imagery when deploying; it is gitignored.

## Validation

Tests cover numeric encoding, gutters, conservative descendant metadata, paging,
CPU/GPU binding parity during height fades, corrupt-child fallback, cancellation,
LOD-consistent clearance, resume and the eight-buffer limit. Three.js generated
the new WGSL on the CPU. A numerical Apollo 12 surface run reached level 20 with
104 drawn tiles and 34 resident height tiles, sampling approximately -1422.07 m.
Those spot checks do not establish browser frame rate.

Live Chrome visual/GPU validation was blocked because Computer Use was not
approved to access Chrome. Build and numerical tests do not replace that check.
