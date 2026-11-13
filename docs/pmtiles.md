# Publish tiles with PMTiles

The globe reads PMTiles v3 archives directly using HTTP byte ranges. Its tile
geometry, tile coordinates, elevation encoding, gutters, and GPU caches are
unchanged. These archives use the `geographic-polar-v1` spatial profile, not a
Web Mercator map. Existing geographic PMTiles viewers cannot display them as maps.

## Build and publish

Build the targets, then release them. `release` packs every tileset and
regional catalog under `public/data` before exporting:

```sh
uv run tile-service build moon/wac moon/elevation-tiles moon/apollo
uv run tile-service release --output releases/v2
```

`--workspace /another/project` builds and releases another project directory. Packing does not download
rasters or regenerate pixels, and an unchanged tileset reuses its archives. The pinned Python writer is
installed from `uv.lock`; the globe uses the npm `pmtiles` reader.

The packer writes one archive per root. Numeric height tiles are also
partitioned by their existing source-path prefix, so adding regional terrain does
not rewrite the global source archives. Regional imagery is packaged under each
site directory. Height payloads use gzip inside PMTiles; PNG bytes are stored
without additional tile compression. Decoded payloads are unchanged.

Archives have content-hash filenames under `pmtiles/`. Packaging validates input
height/page checksums, builds archives, checks their headers/metadata, and finally
atomically replaces the manifest or regional catalog with archive references.
Readers continue using the previous references if packaging fails. Each manifest
or catalog is its own publication unit; packing everything is not a transaction across bodies.

Repeated packaging fingerprints tile contents and reuses verified existing
archives. Source changes during packaging are rejected. Partial archive files
are removed on ordinary failure; an abrupt process kill may leave `.part` files,
which are not referenced by published manifests. Old completed archives remain
available for readers of previous manifests. Cleanup is deliberately explicit.

## Manifest contract

The [dataset contract](../spec/README.md#storage) defines the `storage` descriptor and how
archives are addressed. Each regional imagery site's descriptor is relative to that site's
directory, and sparse regional height sources include only occupied roots.

The viewer still constructs logical `.png`/`.u16` URLs for cache identity. Its tile
reader resolves those addresses into archive ranges; those individual files need
not be hosted. Manifests without `storage` continue to read loose tiles. A missing
or broken archive does not silently switch to loose files.

Height bounds pages remain ordinary JSON files with their existing hashes. They
are still needed for conservative culling and terrain refinement: PMTiles' byte
index does not replace terrain metadata. Regional imagery availability lists and
attribution also remain in the catalog.

## Hosting and updates

Deploy manifests, catalogs, referenced height metadata pages, and referenced
PMTiles archives. Loose tiles and packaging receipts are not needed for viewing;
retain them locally for resumable generation and archive rebuilding.

The Caddy container provides 206 byte ranges, HEAD/OPTIONS, PMTiles MIME types, exposed
ETags, conditional requests, and CORS. Content-hash archives have a one-year
immutable cache policy; manifests and pages revalidate. Another static host can
serve the same files if it supports ranges and appropriate CORS/validators.

To update data: generate, package, upload new archives/pages, then publish the
new manifest/catalog. Retain old referenced content until existing readers and
caches no longer need it. Height generation clears stale archive references when
publishing changed metadata; `uv run tile-service release` repacks them.

Archiving adds a second copy of build data on disk. It improves publication and
file management; it does not by itself promise faster rendering or fewer HTTP
requests. The reader caches archive directories separately from decoded tiles.
