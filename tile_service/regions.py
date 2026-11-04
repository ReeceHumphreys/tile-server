"""Regional imagery: high-resolution disks blended over a global cube pyramid.

Each region is written under `root/<id>/` and listed in `root/catalog.json`,
which the globe reads alongside the base dataset.
"""

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.warp import reproject

from tile_service import documents
from tile_service.cube import Coverage, face_crs, tile_affine, tile_directions
from tile_service.records import BuildRecord, pick
from tile_service.runtime import (
    atomic_json,
    check,
    gdal_version,
    log,
    read_json,
    sha256,
    validate_imagery,
    write_png,
)
from tile_service.sources import PROVENANCE
from tile_service.tile_store import TileStore

REGION_ID = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def base_identity(base):
    """What a region depends on in its base: the pixels, not the words around them."""
    return {
        "id": base.get("id"),
        "sourceSha256": base.get("source", {}).get("sha256"),
        "radiusMeters": float(base["radiusMeters"]),
        "tileSize": int(base["tileSize"]),
        "gutter": int(base["gutter"]),
        "maxLevel": int(base["maxLevel"]),
        "pipelineVersion": base.get("pipelineVersion"),
    }


def _manifest_hash(base):
    """Older records hashed the whole base manifest (without archive references)."""
    unpacked = {key: value for key, value in base.items() if key != "storage"}
    return hashlib.sha256((json.dumps(unpacked, indent=2) + "\n").encode()).hexdigest()


def _upgrades(base):
    def from_manifest_hash(old):
        """Older records: base manifest hash, and the region's whole description."""
        if old.get("baseManifestSha256") != _manifest_hash(base):
            return None
        config = old.get("sourceConfig") or {}
        upgraded = pick(old, ("pipelineVersion", "sourceSha256"))
        upgraded["base"] = base_identity(base)
        upgraded.update(pick(old, ("latitudeDeg", "longitudeDeg", "radiusMeters", "maxLevel")))
        upgraded.update(pick(config, ("overview", "featherMeters")))
        upgraded["gdal"] = old.get("gdal")
        return upgraded

    return (from_manifest_hash,)


def default_level(base, native_resolution):
    """Deepest level whose pixels are no finer than the source's."""
    radius, size = float(base["radiusMeters"]), int(base["tileSize"])
    return int(math.ceil(math.log2(2 * radius / (size * native_resolution))))


def _read_image(store, tile, size):
    data = store.read(f"{tile.key}.png")
    if data is None:
        raise RuntimeError(f"Base tile missing: {tile.key} in {store.directory}")
    with MemoryFile(data) as memory, memory.open() as dataset:
        if dataset.width != size or dataset.height != size or dataset.count != 4:
            raise RuntimeError("Base tile dimensions/channels do not match manifest")
        planes = dataset.read()
    return np.ascontiguousarray(np.moveaxis(planes, 0, -1))


def _complete(root, region_id, tiles):
    """Every tile of a region exists, loose or in the archives its catalog entry lists."""
    catalog = root / "catalog.json"
    listed = read_json(catalog)["sites"] if catalog.exists() else []
    storage = next((s.get("storage") for s in listed if s["id"] == region_id), None)
    store = TileStore(root / region_id, storage)
    try:
        return all(store.has(f"{tile.key}.png") for tile in tiles)
    finally:
        store.close()


def _bilinear(image, xs, ys, band):
    limit = image.shape[0] - 1
    xs = np.clip(xs, 0, limit)
    ys = np.clip(ys, 0, limit)
    x0 = np.floor(xs).astype(np.int32)
    y0 = np.floor(ys).astype(np.int32)
    x1 = np.minimum(x0 + 1, limit)
    y1 = np.minimum(y0 + 1, limit)
    fx = xs - x0
    fy = ys - y0
    plane = image[:, :, band]
    return (1 - fy) * ((1 - fx) * plane[y0, x0] + fx * plane[y0, x1]) + fy * (
        (1 - fx) * plane[y1, x0] + fx * plane[y1, x1]
    )


def _warp_tile(src, address, width, radius, size, gutter):
    frame = np.zeros((src.count + 1, width, width), np.uint8)
    reproject(
        source=rasterio.band(src, list(range(1, src.count + 1))),
        destination=frame,
        src_nodata=src.nodata,
        dst_transform=tile_affine(
            address.face, address.level, address.x, address.y, size, gutter, radius
        ),
        dst_crs=face_crs(address.face, radius),
        resampling=Resampling.average,
        dst_alpha=src.count + 1,
        num_threads=1,
        warp_mem_limit=128,
    )
    return frame


# shortcut: a single gain, not a spatially varying photometric fit. Upgrade when
# a site still shows a brightness ramp the mean cannot remove.
def _match_gain(
    src, bands, addresses, base_tiles, stats_level, base_level, size, gutter, radius, coverage
):
    width = size + 2 * gutter
    total_over = 0.0
    total_under = 0.0
    count = 0
    backgrounds = {}
    for address in addresses:
        if address.level != stats_level:
            continue
        check()
        frame = _warp_tile(src, address, width, radius, size, gutter)
        factor = 1 << (address.level - base_level)
        local_x = address.x % factor
        local_y = address.y % factor
        ancestor = address.ancestor(base_level)
        if ancestor.key not in backgrounds:
            backgrounds[ancestor.key] = _read_image(base_tiles, ancestor, width)
        fallback = backgrounds[ancestor.key]
        rows, cols, vectors = tile_directions(address, size, gutter)
        alpha = frame[bands]
        valid = alpha >= 250
        distance = coverage.distance(vectors, radius)
        inside = (coverage.radius - distance) / coverage.feather >= 0.98
        mask = valid & inside
        if not np.any(mask):
            continue
        px = gutter + (local_x * size + cols - gutter + 0.5) / factor - 0.5
        py = gutter + ((factor - 1 - local_y) * size + rows - gutter + 0.5) / factor - 0.5
        used = 1 if bands == 1 else 3
        over = np.zeros(mask.shape, np.float64)
        under = np.zeros(mask.shape, np.float64)
        for band in range(used):
            source_band = 0 if bands == 1 else band
            over += frame[source_band]
            under += _bilinear(fallback, px, py, band)
        total_over += float((over[mask] / used).sum())
        total_under += float((under[mask] / used).sum())
        count += int(mask.sum())
    if count < 16 or total_over <= 0:
        return 1.0
    return min(4.0, max(0.05, total_under / total_over))


def _write_region_tile(
    src, bands, address, path, fallback, base_level, size, gutter, radius, coverage, gain
):
    width = size + 2 * gutter
    frame = _warp_tile(src, address, width, radius, size, gutter)
    factor = 1 << (address.level - base_level)
    local_x = address.x % factor
    local_y = address.y % factor
    rows, cols, vectors = tile_directions(address, size, gutter)
    px = gutter + (local_x * size + cols - gutter + 0.5) / factor - 0.5
    py = gutter + ((factor - 1 - local_y) * size + rows - gutter + 0.5) / factor - 0.5
    distance = coverage.distance(vectors, radius)
    feather = coverage.weight(distance)
    alpha = frame[bands] / 255 * feather
    under_alpha = _bilinear(fallback, px, py, 3) / 255
    final_alpha = alpha + under_alpha * (1 - alpha)
    output = np.zeros((4, width, width), np.uint8)
    for band in range(3):
        under = _bilinear(fallback, px, py, band)
        over = frame[0 if bands == 1 else band] * gain
        mixed = np.where(
            final_alpha > 0,
            (over * alpha + under * under_alpha * (1 - alpha)) / np.maximum(final_alpha, 1e-12),
            0,
        )
        output[band] = np.clip(np.rint(mixed), 0, 255).astype(np.uint8)
    output[3] = np.clip(np.rint(final_alpha * 255), 0, 255).astype(np.uint8)
    write_png(path, output, 3)


def build(source, base_path, root, config, latitude, longitude, extent, max_level=None):
    """Build (or resume) one region and add it to `root/catalog.json`.

    `config` describes the region: `id`, `name`, provenance, and optionally an
    `overview` disk, `featherMeters` and `replacesRegion`. Returns its entry.
    """
    region_id = config.get("id") or ""
    if not region_id or not set(region_id) <= REGION_ID:
        raise RuntimeError("Invalid region ID")
    missing = [
        key for key in ("name", "credit", "sourcePage") if not isinstance(config.get(key), str)
    ]
    if missing or not (config.get("nativeResolutionMeters") or 0) > 0:
        raise RuntimeError(
            f"Region {region_id} needs name, credit, sourcePage and nativeResolutionMeters "
            f"for the globe (missing {', '.join(missing) or 'nativeResolutionMeters'})"
        )
    source, base_path, root = Path(source), Path(base_path), Path(root)
    out = root / region_id
    base = read_json(base_path)
    radius = float(base["radiusMeters"])
    size = int(base["tileSize"])
    gutter = int(base["gutter"])
    base_level = int(base["maxLevel"])
    if not (
        base.get("projection") == "normalized-cube"
        and math.isfinite(radius)
        and radius > 0
        and 16 <= size <= 2048
        and 1 <= gutter <= 16
        and 0 <= base_level <= 15
    ):
        raise RuntimeError("Invalid base imagery manifest")
    if max_level is None:
        max_level = default_level(base, float(config.get("nativeResolutionMeters") or 1))
    coverage = Coverage(
        latitude,
        longitude,
        extent,
        radius,
        base_level,
        max_level,
        overview=config.get("overview"),
        feather=config.get("featherMeters"),
    )
    addresses = coverage.tiles
    with rasterio.open(source) as src:
        validate_imagery(src, radius)
        bands = src.count
        identity = {
            "pipelineVersion": 1,
            "sourceSha256": sha256(source),
            "base": base_identity(base),
            "latitudeDeg": latitude,
            "longitudeDeg": longitude,
            "radiusMeters": extent,
            "maxLevel": max_level,
            "overview": config.get("overview"),
            "featherMeters": config.get("featherMeters"),
            "gdal": gdal_version(),
        }
        BuildRecord(out, identity, upgrades=_upgrades(base))
        if (out / "manifest.json").exists() and _complete(root, region_id, addresses):
            log(f"Up to date: {out}")
            return _republish(root, config, base)
        base_tiles = TileStore(base_path.parent, base.get("storage"))
        backgrounds = {}
        stats_level = base_level + 1
        while stats_level < max_level and 2 * radius / ((1 << stats_level) * size) > 80:
            stats_level += 1
        gain = _match_gain(
            src,
            bands,
            addresses,
            base_tiles,
            stats_level,
            base_level,
            size,
            gutter,
            radius,
            coverage,
        )
        log(f"{region_id} brightness scale {gain}")
        for index, address in enumerate(addresses, start=1):
            check()
            path = out / f"{address.key}.png"
            if not path.exists():
                ancestor = address.ancestor(base_level)
                if ancestor.key not in backgrounds:
                    backgrounds[ancestor.key] = _read_image(base_tiles, ancestor, size + 2 * gutter)
                _write_region_tile(
                    src,
                    bands,
                    address,
                    path,
                    backgrounds[ancestor.key],
                    base_level,
                    size,
                    gutter,
                    radius,
                    coverage,
                    gain,
                )
            if index % 100 == 0:
                log(f"{index}/{len(addresses)} regional tiles ready")
    return _publish(root, base, config, coverage, identity, gain)


def _provenance(entry, config):
    """`entry` with its descriptive fields replaced by the current ones."""
    source = {**entry.get("source", {}), **{key: config.get(key) for key in PROVENANCE}}
    if "components" in config:
        source["components"] = config["components"]
    return {**entry, "name": config.get("name"), "source": source}


def _republish(root, config, base):
    """Refresh provenance on a finished region; tiles and archives are untouched."""
    region_id = config["id"]
    manifest_path = root / region_id / "manifest.json"
    manifest = read_json(manifest_path)
    updated = _provenance(manifest, config)
    if updated != manifest:
        atomic_json(manifest_path, updated)
    catalog_path = root / "catalog.json"
    catalog = read_json(catalog_path)
    if region_id not in {site["id"] for site in catalog["sites"]}:
        raise RuntimeError(f"Finished region {region_id} is missing from {catalog_path}")
    sites = [_provenance(s, config) if s["id"] == region_id else s for s in catalog["sites"]]
    if sites != catalog["sites"]:
        updated = {**catalog, "sites": sites}
        documents.validate(updated, base)
        atomic_json(catalog_path, updated)
        log(f"Republished provenance for {region_id}")
    return next(site for site in sites if site["id"] == region_id)


def _publish(root, base, config, coverage, identity, gain):
    """Write a freshly built site's manifest and catalog entry."""
    region_id = config["id"]
    out = root / region_id
    published = {
        "id": region_id,
        "name": config.get("name"),
        "latitudeDeg": identity["latitudeDeg"],
        "longitudeDeg": identity["longitudeDeg"],
        "radiusMeters": identity["radiusMeters"],
        "maxLevel": identity["maxLevel"],
    }
    if coverage.description is not None:
        published["overview"] = coverage.description
    published["featherMeters"] = coverage.feather
    provenance = {key: config.get(key) for key in PROVENANCE}
    if "components" in config:
        provenance["components"] = config["components"]
    provenance["colorMatchGain"] = gain
    provenance["sha256"] = identity["sourceSha256"]
    published["source"] = provenance
    published["tiles"] = [tile.key for tile in coverage.tiles]
    catalog = documents.regional_catalog(base)
    catalog_path = root / "catalog.json"
    old = read_json(catalog_path) if catalog_path.exists() else None
    if old is not None:
        if any(old.get(key) != catalog[key] for key in documents.CATALOG_BASE_KEYS):
            raise RuntimeError("Existing regional catalog uses a different base dataset")
        replaced = config.get("replacesRegion")
        catalog = {**old, "sites": []}
        for site in old.get("sites", []):
            if site["id"] == region_id:
                catalog["sites"].append(published)
            elif site["id"] != replaced:
                catalog["sites"].append(site)
    if published not in catalog["sites"]:
        catalog["sites"].append(published)
    documents.validate(catalog, base)
    atomic_json(catalog_path, catalog)
    # Last: this file marks the region finished, so a failed catalog write can retry.
    atomic_json(out / "manifest.json", published)
    log(f"Published {catalog_path} ({len(coverage.tiles)} tiles for {region_id})")
    return published
