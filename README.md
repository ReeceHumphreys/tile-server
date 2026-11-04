# tile-service

Build and publish planetary imagery and elevation as PMTiles. Python processes
the data offline; a small Caddy container serves a read-only static release.
No Node runtime, custom HTTP server, or adjacent renderer checkout is required.

## Build data

Install [uv](https://docs.astral.sh/uv/) and curl, then:

```sh
uv sync --locked
uv run tile-service list                      # every build target and its status
uv run tile-service build moon/wac --dry-run  # sources, sizes and outputs; no downloads
uv run tile-service build moon/wac --max-level 2 --output public/data/moon/wac-preview
uv run tile-service build moon/wac moon/elevation moon/elevation-tiles
uv run tile-service release --output published
```

Four commands cover everything:

| Command   | Does                                                                                      |
| --------- | ----------------------------------------------------------------------------------------- |
| `list`    | Shows the targets in `datasets/catalog.json` and which already exist                      |
| `build`   | Downloads and verifies a target's sources, then builds it; reruns resume or just verify   |
| `release` | Packs every built tileset into PMTiles and exports a new, self-contained release directory |
| `upload`  | Uploads a release to S3 under `releases/<id>/`, resumably and immutably                   |

A target is named after its output under `public/data` (`moon/wac`,
`earth/sites`, …). In `datasets/catalog.json`, each target is a **layer** built
from a **source**:

| Layer             | Builds                               | Needs                     |
| ----------------- | ------------------------------------ | ------------------------- |
| `imagery`         | a global PNG cube pyramid            | `source`                  |
| `grid`            | a global equirectangular DEM grid    | `source`, `width`, `height` |
| `heights`         | a global uint16 height pyramid       | `grid` (a grid target)    |
| `imagery-regions` | detail disks blended over imagery    | `base`, `sites`           |
| `height-regions`  | detail disks blended into heights    | `base`, `sites`           |

Sources are `download` (pinned URL, size, ETag and receipt), `mosaic` (several
downloads joined by a VRT), `wmts` (stitched EOxCloudless tiles) and `naip`
(a site disk cut from pinned NAIP quads); `build --source FILE` substitutes a
local raster. A sites file lists regional sites; a site may carry its own
`source`. The other `datasets/*.json` files pin source URLs, sizes and
checksums. A new body, site or regional pack is a JSON change.

A build's identity is what determines its bytes (source hash, geometry,
levels, toolchain). Editing titles or credits republishes manifests on the
next build without touching tiles or packed archives.
Regional targets accept `--site`, and `--max-level`/`--radius-meters` override
defaults. `release` exports only referenced archives and metadata, verifies
their hashes, and validates every manifest and catalog with the same rules as
the globe's parsers. Once a dataset is packed its loose tiles may be deleted:
later builds, including regions over it, read its tiles from the PMTiles
archives. A build that retires those archives, such as adding a height region,
first writes the deleted tiles back so the dataset can be packed again.

### Workspaces and the library

Everything happens in a workspace directory (`--workspace`, default `.`):

```text
datasets/      your own dataset files, sites files and catalog.json targets
data/raw/      downloaded sources; dataset sourceFile and raw paths are relative to it
public/data/   resumable builds, one directory per target
```

Files in the workspace's `datasets/` add to, or override by name, the ones
packaged with tile-service, so a project can define its own bodies and sites
without editing this repository. The same operations are a Python interface:

```python
from tile_service import Workspace

ws = Workspace("~/planet-data")
ws.build("moon/wac", "moon/elevation-tiles")
ws.release("releases/v3")
ws.upload("releases/v3", bucket="planet-tiles", release_id="v3")
```

`data/`, `public/data/` and release directories are gitignored.
The Python version, dependencies, test configuration, and Ruff settings live in
`pyproject.toml`; `uv.lock` locks the complete dependency set.

## Deploy

```sh
docker compose up -d --build --wait
curl http://127.0.0.1:8787/healthz
```

The default mount is `./published`, which must already exist. The container runs
without root privileges, with a read-only filesystem and release mount. It provides
HTTP ranges, CORS, ETags, health checks, and JSON access logs. No datasets are baked
into the image. Put your HTTPS reverse proxy in front when exposing it remotely.

`PORT`, `BIND_ADDRESS`, `TILE_ROOT`, and `CORS_ORIGIN` configure Compose.
For a new version, publish to a new directory and recreate the service:

```sh
uv run tile-service release --output releases/v2
TILE_ROOT=./releases/v2 docker compose up -d --force-recreate --wait
```

Keep the chosen `TILE_ROOT` in your deployment environment (or `.env`) for future
Compose commands. See [deployment details](docs/deployment.md), including rollback,
containerized processing, and checks against the real HTTP server.

## Consumers

The [globe viewers](../globes) and [Basilisk visualizer](../visualizer) use the
running service at `http://127.0.0.1:8787` by default through their development
and preview proxies. Start Compose separately; consumers never start or stop
the service. Imagery, elevation, regional catalogs, and secondary-body imagery
read the published PMTiles release.

For independently hosted frontends, set `VITE_TILE_BASE` to the browser-accessible
file-service base URL when building each app. Same-origin `/data/...` reverse
proxies also work. App-owned assets such as night lights remain with each app.

## Check

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

CI runs these checks and tests both container targets. The optional
`TILE_TEST_URL` enables HTTP contract tests against an actual deployment.
The globe's existing manifest paths and cube tile coordinates are unchanged.

See [docs/data.md](docs/data.md), [docs/tile-generation.md](docs/tile-generation.md),
and [docs/elevation-tiles.md](docs/elevation-tiles.md). See
[docs/pmtiles.md](docs/pmtiles.md) for the archive format and publication contract.
