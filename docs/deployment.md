# Deployment

The service has two responsibilities with separate runtimes:

```text
Python CLI → resumable tile builds → PMTiles → static release directory
                                                   ↓ read-only mount
                                            Caddy → globe clients
```

`pyproject.toml` and `uv.lock` define the Python tooling. The Dockerfile's `tools`
target installs that package and its dataset catalogs, including curl and the
native libraries needed by rasterio. The default `runtime` target contains Caddy
and its configuration, with no Python environment or source data. Base Python and
Caddy images are pinned by digest. Update those digests and the uv version as
part of dependency maintenance, then rerun the image and HTTP checks.

## Publish a release

```sh
uv sync --locked
# Run the required fetch/generation commands first.
uv run tile-service pack --all
uv run tile-service publish --output published
docker compose up -d --build --wait
```

`publish` scans `public/data/<body>/<dataset>/manifest.json` and `catalog.json`.
`--root` selects another build root. It rejects unpacked cube datasets, missing
files, paths escaping the root, and mismatched archive/page checksums. It exports
only manifests, catalogs, referenced PMTiles, and height metadata pages under
`data/`, preserving all existing request paths. `release.json` describes the release.

It stages the release privately and renames it only after export completes.
Existing release directories are never overwritten. Public files/directories
receive permissions suitable for a non-root static server. Already readable,
immutable archives are hard-linked on the same filesystem to avoid a second data
copy; cross-filesystem or private-mode archives are copied. Metadata is copied,
so later generation cannot change a deployed manifest. Do not modify completed
content-addressed archives in place.

Build directories still contain loose tiles, source receipts, and working state.
They are not deployment directories. Legacy global height blobs are also omitted;
deployed clients should use the tiled height manifests. Viewer-specific overview
images and night-light assets remain the viewer application's responsibility.

## Runtime configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `TILE_ROOT` | `./published` | Complete release directory mounted at `/srv` |
| `BIND_ADDRESS` | `127.0.0.1` | Host bind address; use the reverse proxy's intended interface |
| `PORT` | `8787` | Host port; Caddy listens on 8080 inside the container |
| `CORS_ORIGIN` | `*` | Allowed browser origin; may be one exact origin |

The data mount must exist; Compose does not silently create an empty directory.
Caddy serves GET/HEAD/OPTIONS, byte ranges and conditional requests. Hash-named
archives have a one-year immutable cache lifetime. JSON metadata revalidates.
Transport compression is not enabled for archive responses, preserving offsets.
Directory browsing and SPA/HTML fallbacks are not enabled.

The container uses UID/GID 10001, drops capabilities, disables privilege gains,
and has a read-only root filesystem and release mount. Its only writable space is
a bounded `/tmp`. Caddy's unneeded privileged-port file capability is removed at
build time. JSON access logs go to stdout; Compose rotates them. `/healthz` tests
the HTTP process; the container health check reads `/release.json`, confirming the
release is mounted. `docker compose up --wait` checks startup health.

TLS is expected at your existing reverse proxy or load balancer. This container
does not obtain certificates. On a single host, replace a release with:

```sh
uv run tile-service publish --output releases/2026-10-25
TILE_ROOT=./releases/2026-10-25 docker compose up -d --force-recreate --wait
```

Roll back by recreating the service with the previous `TILE_ROOT`. Persist that
variable in the deployment environment or a gitignored `.env`. Keep previous
releases for rollback; clients may need to reload metadata after a release switch.
Compose recreation briefly interrupts serving; use a proxy and parallel instances
if uninterrupted updates are a requirement. Back up published releases or preserve
the sources and build state needed to reproduce them.

## Processing inside a container

```sh
docker compose --profile tools build tools
LOCAL_UID=$(id -u) LOCAL_GID=$(id -g) docker compose --profile tools run --rm tools polar --info
LOCAL_UID=$(id -u) LOCAL_GID=$(id -g) docker compose --profile tools run --rm tools pack --all
```

The tools service mounts `data/`, `public/`, and read-only `datasets/` under `/work`.
Match `LOCAL_UID`/`LOCAL_GID` to the host owner for write access on Linux.
Dataset catalogs are also bundled in the installed Python wheel, so built-in
commands work outside a checkout. Container commands do not need globes or Node.
To publish with the tools image, explicitly mount the output directory's parent:

```sh
docker run --rm --user "$(id -u):$(id -g)" \
  -v "$PWD/public:/work/public:ro" -v "$PWD/releases:/work/releases" \
  tile-service-tools publish --output releases/v1
```

Build that tag with `docker build --target tools -t tile-service-tools .` and
create `releases/` first. Serving does not require building the tools image.

## Validation

```sh
uv run pytest
uv run python -m tests.make_release /tmp/tile-release
TILE_ROOT=/tmp/tile-release PORT=18787 docker compose -p tile-smoke up -d --build --wait
TILE_TEST_URL=http://127.0.0.1:18787 uv run pytest -m deployment
docker compose -p tile-smoke down
```

The smoke release uses small analytic rasters. HTTP tests run against Caddy and
check ranges, MIME type, ETags, conditional reads, CORS, HEAD, methods, and missing
files. Python regression tests cover projections, seams/poles/gutters, nodata,
determinism, interrupted builds, downloads, regional refinements, numeric heights,
PMTiles payload equality, reuse, publication, and package/catalog independence.
They need no sibling checkout, credentials, or external dataset downloads. A local
LOLA equivalence check runs only when its existing source is available.
