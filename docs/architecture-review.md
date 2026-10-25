# Tile service and globe architecture review

Reviewed 2026-10-25 against the current working trees of `tile-service` and
`../globes`. This is a design review, not an implementation or performance study.

Subsequent implementation: [PMTiles publication](pmtiles.md) adds archive packaging
and the globe tile reader without changing the cube geometry. Findings below
describe the pre-integration snapshot; transport and hosting changes are now implemented.

## Verdict

The cube-sphere renderer is a coherent specialized implementation. The evidence
does not justify replacing it simply because mainstream web maps use another
grid. Its principal architectural limitation is that source addressing, streaming,
and GPU bindings assume cube tiles throughout. The service currently produces a
renderer-specific product, rather than a general catalog of tiled datasets.

Keep the existing surface as a supported implementation. Introduce a narrow
boundary for tile bytes and explicit dataset descriptions. Validate standard
imagery consumption before deciding whether to retain cube geometry, add a
geographic surface, or keep offline cube conversion as the preferred fast path.
PMTiles is a promising delivery format; it does not settle those decisions.

## Current architecture and strengths

The service uses a Node CLI wrapper around Python/rasterio/GDAL processing.
`scripts/data.ts` creates a Python launcher; the test names still say “C++”, so
those names do not describe the current implementation.

Imagery is reprojected into six gnomonic face grids and written as guttered PNGs.
Elevation becomes guttered little-endian uint16 tiles, with conservative terrain
ranges and paged metadata. The HTTP process serves already generated files.

The globe selects cube quadtree patches, draws one shared instanced grid, and
binds resident imagery and height arrays. Source and geometry levels are not
strictly identical: a fine mesh patch can sample a coarser resident ancestor.
However, both use the same face grid and ancestry.

Useful architecture already present:

- The engine accepts replaceable `GlobeSurface` implementations. Cube geometry is
  not mandatory at the outer engine boundary (`globes/src/composition.ts:100`).
- Camera-relative anchors and a stable shader expression address precision at
  planetary scale (`rendering/tile-instances.ts:111`, `shaders/vertex.wgsl:33`).
- Horizon/frustum culling includes terrain bounds; selection respects a fixed
  patch budget (`tiles/visibility.ts`, `tiles/quadtree.ts`).
- Streaming has bounded residency and request concurrency, cancellation,
  ancestor fallback, and fades (`imagery/tile-cache.ts`, `elevation/tile-cache.ts`).
- Height payloads are checked for hash, dimensions, and numeric range before
  admission. Metadata validates conservative parent bounds and page membership.
- Offline generation records provenance and supports interruption/resume.

These are valuable properties to preserve through any projection or storage change.

## Findings, ordered by consequence

### 1. Source layout is embedded in streaming and bindings

**High priority for supporting other tile sources or consumers.**

`globes/src/imagery/manifest.ts:68` requires `normalized-cube`, PNG, fixed axes,
and the exact `{face}/{level}/{x}/{y}.png` template. In
`imagery/tile-cache.ts:157`, scheduling constructs those URLs directly. The same
cache uses cube addresses for availability, ancestry, prefetch, and UV bindings.
Height manifests similarly require cube addressing and a fixed numeric encoding.

Consequently, a PMTiles URL is not currently substitutable for a loose-file URL,
and a geographic source cannot be supported by replacing only `fetch`.

First separate byte retrieval from decoding and residency. A cube tile source
should resolve an address into bytes without exposing filename construction to
the cache. Add PMTiles as one implementation of that boundary. Keep projection
mapping separate: a byte reader cannot turn Mercator tiles into cube textures.
Avoid a universal provider framework until a second real source demonstrates
the required operations.

### 2. Coordinate semantics are adequate for spherical visualization, not general GIS

**High priority if Earth accuracy or scientific data interoperability is required.**

`globes/src/body.ts:1` models a named sphere and planetocentric coordinates.
Compatibility checks compare name and radius. The service documentation explicitly
places Earth longitude/latitude and EGM2008 orthometric heights onto a sphere;
this is a visualization convention, not an ellipsoid/vertical-datum conversion.

Two datasets sharing a body name and radius can still differ in reference frame,
latitude definition, longitude convention, or vertical datum. A catalog intended
for multiple clients must describe these explicitly. Preserve original CRS and
source provenance, and record the transformation or approximation used to build
each representation. PMTiles bounds are not a substitute for that metadata.

The current height encoding also has a fixed range of -10000 to +22767.5 m.
Earth trench clamping is documented and tested. General source support should
describe encoding scale/offset and treatment of out-of-range/nodata samples,
rather than infer that all numeric elevation uses the current lunar encoding.

### 3. Geometry detail has no measured terrain-error target

**Medium priority for close terrain quality and predictable performance.**

`globes/src/tiles/lod.ts:51` explicitly describes projected grid-cell size, not
measured elevation error. Conservative height ranges support visibility and
distance estimation; they do not bound the difference between a coarse mesh and
a detailed height surface. This can spend geometry on smooth areas or fail to
provide a chosen error guarantee on rugged areas. It is not evidence of an
observed rendering defect.

For terrain demanding a defined quality target, generate conservative geometric
error relative to the actual reconstruction and use projected error for selection.
Include spherical curvature and source resolution. Keep explicit geometry and
imagery quality targets rather than treating their level numbers as equivalent.
Verify convergence and budget behavior on flat, mountainous, and polar fixtures.

### 4. Regional publication needs explicit dependency/version semantics

**Medium priority for independently updated datasets.**

`globes/src/imagery/regions.ts:24` ties regional imagery to base identity,
depth, tile dimensions, and gutter, and rejects overlapping regional addresses.
`pipeline/heights.py:350` freezes the underlying height manifest/pages when baking
a regional refinement. Changing the lower layer does not automatically regenerate
an already blended higher layer. These are valid current constraints, but do not
yet define a general independently composable layer system.

Publish immutable dataset releases with base and regional dependencies pinned.
Define overlap priority and availability before allowing competing refinements.
Archive boundaries should follow update boundaries; appending one Apollo site
should not require rewriting an entire planetary archive. PMTiles archives are
read-only and updates require rewriting the archive [1]. Keep terrain bounds
consistent with the exact published combination of base and refinements.

### 5. The server supports ranges but lacks the full intended publication contract

**Medium priority before using it as the archive origin.**

`scripts/serve.ts:19` allows `Range`, but does not emit ETags, permit `If-Match`
through CORS, or expose ETags. It also has no explicit cache policy or PMTiles
MIME mapping. Existing 206 responses establish basic range support; they do not
validate archive replacement consistency, directory caching, proxy behavior, or
browser PMTiles access. Protomaps documents ETag/If-Match CORS configuration [2].

Use versioned immutable archive URLs, cacheable dataset payloads, and a separately
refreshable catalog. Add appropriate validators, conditional handling, CORS, and
the `application/vnd.pmtiles` type when implementing archive serving. A standard
static host/object store is also suitable; no map-specific runtime is required.

### 6. Transient streaming failures can remain sticky while visible

**Medium priority for network reliability.**

Imagery failures are retained in a set until the tile is no longer desired
(`globes/src/imagery/tile-cache.ts:179` and `:467`).
Height tiles and pages follow a similar pattern. Parent fallback preserves
coverage, but a temporary failure can leave detail unavailable while the camera
stays on that region.

Add bounded retries with backoff for transient transport failures, while keeping
invalid/corrupt payload failures distinct. If PMTiles shares directory requests
between tile consumers, cancellation must not let one cancelled consumer disrupt
another. Test request deduplication, failed ranges, archive changes, and retry
behavior during rapid camera motion.

### 7. Pipeline scale and repository independence have limits

**Lower priority for present datasets; relevant before expanding scope.**

`pipeline/tiles.py:122` prepares whole-face raster caches and rejects dimensions
above 131072 pixels. That is a bounded offline implementation, not an unlimited
planetary tiling backend. Regional/windowed generation offers a path for deeper
coverage; packing output files alone does not reduce reprojection work.

The service tests and Apollo/polar commands import sibling globe code. A clean
checkout is therefore not fully independent. Move shared schema validation into
a versioned contract package, or retain service-owned validation and explicit
cross-repository compatibility tests. Do not make all service tests require an
unversioned adjacent checkout. Rename stale C++ test labels during routine cleanup.

## What established approaches demonstrate

Cesium exposes both Web Mercator and geographic tiling schemes [3,4], and terrain
providers supporting heightmaps and quantized meshes [5]. This demonstrates that
globe rendering does not require cube-face source data. It does not demonstrate
that our cube surface is incorrect or that Mercator should become our planetary
default. The conventional global Mercator square excludes the poles, which are
an explicit requirement of the lunar datasets.

PMTiles indexes a one-root square quadtree using Z/X/Y-derived Hilbert IDs [6].
Six cube-face archives can reuse that indexing, but constitute a custom spatial
profile understood by our client. Numeric heights are also a custom payload
profile. Ordinary map viewers will not interpret those products automatically.
Even a geographic scheme needs explicit checking: Cesium's default geographic
scheme has two level-zero tiles, unlike the one-root PMTiles address space.
“Geographic PMTiles” therefore needs a defined mapping and client agreement;
it should not be presented as an automatically interoperable standard.

## Options and decision

| Option | Benefit | Cost | Review recommendation |
| --- | --- | --- | --- |
| Keep matching cube tiles; add archives | Smallest migration, full poles, reuse terrain work | Custom sources and offline conversion persist | Keep as supported optimized path |
| Cube mesh sampling geographic/Mercator sources | Reuse mesh and accept external sources | Overlap lookup, nonlinear UVs, multiple bindings, seams and bounds | Prototype imagery before committing |
| Geographic mesh with matching geographic tiles | Source/mesh alignment with a conventional globe layout | Another surface implementation; polar grid and precision considerations | Compare if direct sources are a core requirement |
| Replace engine with Cesium or another mature globe | Broad existing geospatial capabilities | Different rendering/integration constraints and migration effort | Evaluate only if those capabilities outweigh custom WebGPU requirements |

Do not select the most general option solely to support hypothetical clients.
For a private Moon/Earth/Mars renderer, matching cube tiles is a reasonable
product. For a reusable mapping service, cube-only output should be a named
representation rather than the definition of a dataset.

## Proposed implementation sequence and acceptance criteria

1. Define dataset identity, coordinate semantics, representation, encoding,
   availability, provenance, and immutable release dependencies. Preserve current
   cube manifests through an adapter; do not rebuild all datasets first.
2. Introduce a cube-addressed tile-byte reader and move loose-file URL resolution
   behind it. Keep request scheduling, decode, residency, and GPU ownership clear.
3. Package one imagery pyramid and one elevation fixture into face PMTiles
   archives. Preserve gutters, orientation, height metadata, hashes, and ancestry.
   Validate exact decoded equivalence and rendered seams, not just archive validity.
4. Prove a standard Web Mercator imagery source on a bounded Earth region. Compare
   cube resampling/offline conversion with direct sampling; retain explicit polar
   fallback. If a geographic mesh is simpler than arbitrary cube/source overlap,
   prototype it through the existing `GlobeSurface` boundary. Do not port numeric
   terrain until imagery establishes the projection and residency design.
5. Decide the default publication profiles from those results. Add geometric
   error metadata and richer geodesy according to the chosen product requirements.

For steps 3–4 use identical camera paths, source coverage, output quality, and
cache budgets. Measure cold and warm time to useful detail; p50/p95 frame and CPU
times; request count and bytes (including directories); GPU/CPU memory; decoding
and upload time; failures/retries; poles/date line/face-edge behavior; build,
packaging, copy/upload, and incremental-update costs. Check both localhost and
the intended remote hosting path. Do not infer an FPS improvement from file count
or a shader-kernel benchmark. Keep the current path until the new path preserves
coverage/correctness and has measured acceptable performance.

## Validation performed

- All 20 tile-service tests passed across the initial run and an isolated rerun
  of the HTTP test. The first HTTP run was blocked by sandbox localhost binding;
  the permitted rerun passed.
- 36 selected globe tests passed: adaptive LOD, face UV, coverage, imagery cache,
  height tiles, tile instances, and visibility.
- Globe `npm run typecheck` passed.
- No browser/GPU session or comparative projection/archive benchmark was run.
  Existing terrain-cache measurements are kernel experiments, not evidence of
  end-to-end architecture superiority. This review leaves runtime code unchanged.

## External references

1. [PMTiles concepts and read-only update model](https://docs.protomaps.com/pmtiles/)
2. [PMTiles hosting, range requests, and CORS](https://docs.protomaps.com/pmtiles/cloud-storage)
3. [Cesium WebMercatorTilingScheme](https://cesium.com/learn/cesiumjs/ref-doc/WebMercatorTilingScheme.html)
4. [Cesium GeographicTilingScheme](https://cesium.com/learn/cesiumjs/ref-doc/GeographicTilingScheme.html)
5. [CesiumTerrainProvider](https://cesium.com/learn/cesiumjs/ref-doc/CesiumTerrainProvider.html)
6. [PMTiles v3 specification](https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md)
