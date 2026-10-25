# tile-service

Build and publish planetary imagery and elevation as PMTiles. Python processes
the data offline; a small Caddy container serves a read-only static release.
No Node runtime, custom HTTP server, or adjacent renderer checkout is required.

## Build data

Install [uv](https://docs.astral.sh/uv/) and curl, then:

```sh
uv sync --locked
uv run tile-service --help
uv run tile-service fetch --info
uv run tile-service tiles --max-level 2 --output public/data/moon/wac-preview
uv run tile-service pack --all
uv run tile-service publish --output published
```

Download sources explicitly with `uv run tile-service fetch` before generating
tiles. Source rasters live in `data/raw`; resumable builds live in `public/data`.
`publish` exports only referenced archives and metadata, verifies their hashes,
and creates a new release directory. All three directories are gitignored.
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
uv run tile-service publish --output releases/v2
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
