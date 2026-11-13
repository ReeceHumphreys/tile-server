"""Global imagery: reproject a source raster into four guttered PNG pyramids, one per root."""

import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import reproject
from rasterio.windows import Window

from tile_service import documents
from tile_service.records import BuildRecord
from tile_service.roots import (
    NORTH,
    ROOTS,
    bilinear,
    east_west_pixels,
    global_equirect,
    is_cap,
    pyramid,
    root_crs,
    root_max_level,
    smooth_row,
    tile_affine,
    tile_count,
)
from tile_service.runtime import (
    atomic_json,
    check,
    gdal_version,
    log,
    read_json,
    run_tiles,
    sha256,
    stop,
    tile_pool,
    validate_imagery,
    write_png,
)
from tile_service.tile_store import TileStore

PIPELINE_VERSION = 5
# Rows read from the source for a cap: past the 78.8 degrees its edges reach at their middles.
# Its corners reach 74.2 degrees, and only repeat these rows: no drawing needs them.
CAP_LATITUDE = 78.5


def _tile_path(out, root, level, x, y):
    return out / str(root) / str(level) / str(x) / f"{y}.png"


def _tiles(level):
    return (
        f"{root}/{depth}/{x}/{y}.png"
        for root in ROOTS
        for depth, x, y in pyramid(root_max_level(root, level))
    )


def _available(out, manifest, level):
    """Every tile exists, loose or in the manifest's packed archives."""
    store = TileStore(out, manifest.get("storage"))
    try:
        return all(store.has(tile) for tile in _tiles(level))
    finally:
        store.close()


def _complete(out, root, level):
    addresses = pyramid(root_max_level(root, level))
    return all(_tile_path(out, root, *address).exists() for address in addresses)


def _fill_holes(dataset, src, bands, width, crs, transform):
    # Average can leave holes. Fill only where bilinear still
    # sees source coverage, and keep real nodata.
    alpha_index = bands + 1
    for y in range(0, width, 1024):
        for x in range(0, width, 1024):
            check()
            window_width = min(1024, width - x)
            window_height = min(1024, width - y)
            window = Window(x, y, window_width, window_height)
            alpha = dataset.read(alpha_index, window=window)
            if not np.any(alpha == 0) or not np.any(alpha > 0):
                continue
            shift = rasterio.transform.Affine(
                transform.a,
                transform.b,
                transform.c + x * transform.a + y * transform.b,
                transform.d,
                transform.e,
                transform.f + x * transform.d + y * transform.e,
            )
            fallback = np.zeros((bands + 1, window_height, window_width), np.uint8)
            reproject(
                source=rasterio.band(src, list(range(1, bands + 1))),
                destination=fallback,
                src_nodata=src.nodata,
                dst_transform=shift,
                dst_crs=crs,
                resampling=Resampling.bilinear,
                dst_alpha=bands + 1,
                num_threads=1,
                warp_mem_limit=128,
            )
            keep = (alpha == 0) & (fallback[bands] > 0)
            if not np.any(keep):
                continue
            for band in range(1, bands + 2):
                original = dataset.read(band, window=window)
                original[keep] = fallback[band - 1][keep]
                dataset.write(original, indexes=band, window=window)


def _smooth_high_latitudes(dest, bands, width, transform, radius):
    block = 256
    for y in range(0, width, block):
        check()
        height = min(block, width - y)
        latitudes = [
            math.degrees((transform * (0, y + line + 0.5))[1] / radius) for line in range(height)
        ]
        counts = [east_west_pixels(latitude, width) for latitude in latitudes]
        if max(counts) < 3:
            continue
        window = Window(0, y, width, height)
        for band in range(1, bands + 1):
            pixels = dest.read(band, window=window)
            for line, count in enumerate(counts):
                if count >= 3:
                    pixels[line] = smooth_row(pixels[line], count, "edge")
            dest.write(pixels, indexes=band, window=window)


def _sample_cap(src, dest, root, bands, width, transform, radius):
    """Fill a cap by sampling the source directly, wrapping longitude.

    GDAL's warper smears a polar target where it crosses the source's date line, so a
    whole-world source is read a band of rows at a time and sampled here.
    """
    unit = 1.0 if src.crs.is_geographic else math.radians(1) * radius
    inverse = ~src.transform
    pole = 1 if root == NORTH else -1
    edge_row = (inverse * (0, pole * CAP_LATITUDE * unit))[1]
    pole_row = (inverse * (0, pole * 90 * unit))[1]
    first = max(0, math.floor(min(edge_row, pole_row)) - 1)
    last = min(src.height, math.ceil(max(edge_row, pole_row)) + 2)
    rows = src.read(window=Window(0, first, src.width, last - first))
    valid = np.ones(rows.shape[1:], bool)
    if src.nodata is not None:
        valid = (rows != src.nodata).any(axis=0)
    for line in range(rows.shape[1]):
        latitude = (src.transform * (0, first + line + 0.5))[1] / unit
        count = east_west_pixels(latitude, src.width)
        if count >= 3:
            for band in range(rows.shape[0]):
                rows[band, line] = smooth_row(rows[band, line], count, "wrap")
    for y in range(0, width, 1024):
        for x in range(0, width, 1024):
            check()
            block_width, block_height = min(1024, width - x), min(1024, width - y)
            cols, lines = np.meshgrid(
                x + np.arange(block_width) + 0.5, y + np.arange(block_height) + 0.5
            )
            px, py = transform * (cols, lines)
            colatitude = 2 * np.arctan(np.hypot(px, py) / (2 * radius))
            latitude = pole * (90 - np.degrees(colatitude))
            longitude = np.degrees(np.arctan2(px, -py if pole > 0 else py))
            sx, sy = inverse * (longitude * unit, latitude * unit)
            sx, sy = sx - 0.5, sy - 0.5 - first
            window = Window(x, y, block_width, block_height)
            for band in range(bands):
                colors = np.rint(bilinear(rows[band], sx, sy, wrap=True)).astype(np.uint8)
                dest.write(colors, indexes=band + 1, window=window)
            x0 = np.floor(sx).astype(np.int64) % src.width
            y0 = np.clip(np.floor(sy).astype(np.int64), 0, rows.shape[1] - 2)
            alpha = np.where(valid[y0, x0], 255, 0).astype(np.uint8)
            dest.write(alpha, indexes=bands + 1, window=window)


def _prepare_root(src, path, root, bands, settings):
    if path.exists():
        return
    check()
    scale = 1 << root_max_level(root, settings["level"])
    width = (settings["size"] + 2 * settings["gutter"]) * scale
    if width > 131072:
        raise RuntimeError("Root cache too large; reduce tile size or max level.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(path) + ".tmp")
    crs = root_crs(root, settings["radius"])
    transform = tile_affine(
        root,
        0,
        0,
        0,
        settings["size"] * scale,
        settings["gutter"] * scale,
        settings["radius"],
    )
    with rasterio.open(
        temp,
        "w+",
        driver="GTiff",
        width=width,
        height=width,
        count=bands + 1,
        dtype="uint8",
        crs=crs,
        transform=transform,
        tiled=True,
        compress="lzw",
        bigtiff="IF_SAFER",
    ) as dest:
        colors = [ColorInterp.gray] * bands + [ColorInterp.alpha]
        if bands == 3:
            colors = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        dest.colorinterp = tuple(colors)
        if is_cap(root) and global_equirect(src, settings["radius"]):
            _sample_cap(src, dest, root, bands, width, transform, settings["radius"])
        else:
            reproject(
                source=rasterio.band(src, list(range(1, bands + 1))),
                destination=rasterio.band(dest, list(range(1, bands + 2))),
                src_nodata=src.nodata,
                resampling=Resampling.average,
                dst_alpha=bands + 1,
                num_threads=1,
                warp_mem_limit=128,
            )
            _fill_holes(dest, src, bands, width, crs, transform)
            if not is_cap(root):
                _smooth_high_latitudes(dest, bands, width, transform, settings["radius"])
    check()
    # Overviews come first: a cache under its final name is trusted on resume.
    depth = root_max_level(root, settings["level"])
    if depth:
        with rasterio.open(temp, "r+") as dest:
            dest.build_overviews([1 << i for i in range(1, depth + 1)], Resampling.average)
    check()
    os.replace(temp, path)


def _extract(cache, path, root, level, x, y, bands, settings):
    width = settings["size"] + 2 * settings["gutter"]
    depth = root_max_level(root, settings["level"])
    factor = 1 << (depth - level)
    border = settings["gutter"] * (1 << depth)
    wx = border + (x * settings["size"] - settings["gutter"]) * factor
    wy = border + (((1 << level) - 1 - y) * settings["size"] - settings["gutter"]) * factor
    window = Window(wx, wy, width * factor, width * factor)
    rgba = np.empty((4, width, width), np.uint8)
    for band in range(1, 5):
        source = bands + 1 if band == 4 else (1 if bands == 1 else band)
        rgba[band - 1] = cache.read(
            source,
            window=window,
            out_shape=(width, width),
        )
    if not is_cap(root):
        _extend_world_edges(rgba, x, y, level, settings["gutter"])
    write_png(path, rgba, settings["compression"])


def _extract_batch(addresses, cached, out, root, bands, settings):
    with rasterio.open(cached) as cache:
        for level, x, y in addresses:
            check()
            path = _tile_path(out, root, level, x, y)
            _extract(cache, path, root, level, x, y, bands, settings)


def _extend_world_edges(rgba, x, y, level, gutter):
    # The source stops at the edge of the world, but interpolation still reads the gutter.
    last = (1 << level) - 1
    if x == 0:
        rgba[:, :, :gutter] = rgba[:, :, gutter : gutter + 1]
    if x == last:
        rgba[:, :, -gutter:] = rgba[:, :, -gutter - 1 : -gutter]
    if y == 0:
        rgba[:, -gutter:, :] = rgba[:, -gutter - 1 : -gutter, :]
    if y == last:
        rgba[:, :gutter, :] = rgba[:, gutter : gutter + 1, :]


def _source_metadata(src, digest, source):
    nodata = src.nodata
    transform = list(src.transform)[:6]
    return {
        "url": source.get("sourceUrl"),
        "page": source.get("sourcePage"),
        "credit": source.get("credit"),
        "sha256": digest,
        "crsWkt": src.crs.to_wkt() if src.crs else "",
        "width": src.width,
        "height": src.height,
        "bands": src.count,
        "dtype": "uint8",
        "nodata": None if nodata is None else nodata,
        "transform": transform,
    }


def build(
    source,
    out,
    dataset,
    *,
    tile_size=None,
    gutter=None,
    max_level=None,
    jobs=2,
    png_compression=3,
    keep_cache=False,
):
    """Build (or resume) a pyramid of `source` described by `dataset` into `out`.

    Tile size, gutter and depth default to the dataset's. Returns the manifest.
    """
    source, out = Path(source), Path(out)
    settings = {
        "size": int(tile_size if tile_size is not None else dataset.get("tileSize") or 0),
        "gutter": int(gutter if gutter is not None else dataset.get("gutter") or 0),
        "level": int(max_level if max_level is not None else dataset.get("maxLevel") or 0),
        "jobs": jobs,
        "compression": png_compression,
        "keep": keep_cache,
        "radius": float(dataset.get("radiusMeters") or 0),
    }
    size = settings["size"]
    if not (
        16 <= size <= 2048
        and size & (size - 1) == 0
        and 1 <= settings["gutter"] <= 16
        and 0 <= settings["level"] <= 12
    ):
        raise RuntimeError("Use power-of-two tile size 16..2048, gutter 1..16, max level 0..12")
    if not (isinstance(jobs, int) and 1 <= jobs <= 6):
        raise RuntimeError("Jobs must be 1..6")
    if not 0 <= png_compression <= 9:
        raise RuntimeError("PNG compression must be 0..9")
    if not math.isfinite(settings["radius"]) or settings["radius"] <= 0:
        raise RuntimeError("Invalid body radius")
    with rasterio.open(source) as src:
        validate_imagery(src, settings["radius"])
        bands = src.count
        log("Validating source SHA-256…")
        digest = sha256(source)
        metadata = _source_metadata(src, digest, dataset)
    identity = {
        "pipelineVersion": PIPELINE_VERSION,
        "gdal": gdal_version(),
        "sourceSha256": digest,
        "radiusMeters": settings["radius"],
        "tileSize": size,
        "gutter": settings["gutter"],
        "maxLevel": settings["level"],
        "pngCompression": png_compression,
    }
    BuildRecord(out, identity)
    manifest_path = out / "manifest.json"
    if manifest_path.exists() and _available(out, read_json(manifest_path), settings["level"]):
        log(f"Up to date: {out}")
        return _refresh(manifest_path, _manifest(dataset, settings, metadata, png_compression))
    log(f"{tile_count(settings['level'])} tiles; {jobs} root workers.")
    errors = []
    lock = threading.Lock()

    def run_root(root):
        try:
            check()
            cached = out / ".cache" / f"{root}.tif"
            if not _complete(out, root, settings["level"]):
                log(f"Building root {root}")
                with rasterio.open(source) as src:
                    _prepare_root(src, cached, root, bands, settings)
                addresses = [
                    address
                    for address in pyramid(root_max_level(root, settings["level"]))
                    if not _tile_path(out, root, *address).exists()
                ]
                run_tiles(_extract_batch, addresses, cached, out, root, bands, settings, pool=tiles)
            if not settings["keep"]:
                cached.unlink(missing_ok=True)
            log(f"Root {root} ready")
        except Exception as exc:
            stop.set()
            with lock:
                if not errors:
                    errors.append(exc)

    with tile_pool() as tiles, ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(run_root, ROOTS))
    if errors:
        stop.clear()  # It only stopped this build's roots.
        raise errors[0]
    check()
    manifest = _manifest(dataset, settings, metadata, png_compression)
    atomic_json(manifest_path, manifest)
    log(f"Published {manifest_path}")
    return manifest


def _manifest(dataset, settings, metadata, png_compression):
    manifest = documents.imagery_manifest(
        dataset,
        radius=settings["radius"],
        tile_size=settings["size"],
        gutter=settings["gutter"],
        max_level=settings["level"],
        source=metadata,
        producer={
            "name": "tile-service",
            "pipelineVersion": PIPELINE_VERSION,
            "gdal": gdal_version(),
            "pngCompression": png_compression,
        },
    )
    documents.validate(manifest)
    return manifest


# Fields that name a PMTiles archive; while they hold, packed archives stay valid.
ARCHIVE_KEYS = ("body", "radiusMeters", "id", "format")


def _refresh(path, manifest):
    """Republish provenance on a finished pyramid; tiles and archives are untouched."""
    old = read_json(path)
    storage = old.pop("storage", None)
    if old == manifest:
        return read_json(path)
    if storage and all(old.get(key) == manifest.get(key) for key in ARCHIVE_KEYS):
        manifest["storage"] = storage
    elif storage:
        # These archives are retired, so the next pack needs every loose tile.
        TileStore(path.parent, storage).restore(_tiles(manifest["maxLevel"]))
    atomic_json(path, manifest)
    log(f"Republished provenance in {path}")
    return manifest
