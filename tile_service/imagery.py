"""Global imagery: reproject a source raster into six guttered cube-face PNG pyramids."""

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
from tile_service.cube import face_crs, pyramid, tile_affine
from tile_service.records import BuildRecord, pick
from tile_service.runtime import (
    atomic_json,
    check,
    gdal_version,
    log,
    read_json,
    sha256,
    stop,
    validate_imagery,
    write_png,
)
from tile_service.tile_store import TileStore

PIPELINE_VERSION = 3


def _tile_path(out, face, level, x, y):
    return out / str(face) / str(level) / str(x) / f"{y}.png"


def _tiles(level):
    return (f"{face}/{depth}/{x}/{y}.png" for face in range(6) for depth, x, y in pyramid(level))


def _available(out, manifest, level):
    """Every tile exists, loose or in the manifest's packed archives."""
    store = TileStore(out, manifest.get("storage"))
    try:
        return all(store.has(tile) for tile in _tiles(level))
    finally:
        store.close()


def _complete(out, face, level):
    return all(_tile_path(out, face, *address).exists() for address in pyramid(level))


def _fill_holes(dataset, src, bands, width, crs, transform):
    # Average leaves holes on the polar faces. Fill only where bilinear still
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


def _prepare_face(src, path, face, bands, settings):
    if path.exists():
        return
    check()
    scale = 1 << settings["level"]
    width = (settings["size"] + 2 * settings["gutter"]) * scale
    if width > 131072:
        raise RuntimeError("Face cache too large; reduce tile size or max level.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(path) + ".tmp")
    crs = face_crs(face, settings["radius"])
    transform = tile_affine(
        face,
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
    check()
    # Overviews come first: a cache under its final name is trusted on resume.
    if settings["level"]:
        with rasterio.open(temp, "r+") as dest:
            dest.build_overviews(
                [1 << i for i in range(1, settings["level"] + 1)],
                Resampling.average,
            )
    check()
    os.replace(temp, path)


def _extract(cache, path, level, x, y, bands, settings):
    width = settings["size"] + 2 * settings["gutter"]
    factor = 1 << (settings["level"] - level)
    border = settings["gutter"] * (1 << settings["level"])
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
    write_png(path, rgba, settings["compression"])


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
    """Build (or resume) a cube pyramid of `source` described by `dataset` into `out`.

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
    record = BuildRecord(out, identity, upgrades=(_from_dataset_record, _from_v2(out)))
    manifest_path = out / "manifest.json"
    if record.previous and record.previous.get("pipelineVersion") == 2:
        if not _available(out, read_json(manifest_path), settings["level"]):
            raise RuntimeError("Legacy dataset is incomplete; rebuild into a new output directory.")
        log("Verified complete legacy pyramid; retained unchanged.")
        return read_json(manifest_path)
    if manifest_path.exists() and _available(out, read_json(manifest_path), settings["level"]):
        log(f"Up to date: {out}")
        return _refresh(manifest_path, _manifest(dataset, settings, metadata, png_compression))
    count = 2 * ((1 << (2 * (settings["level"] + 1))) - 1)
    log(f"{count} tiles; {jobs} face workers.")
    errors = []
    lock = threading.Lock()

    def run_face(face):
        try:
            check()
            cached = out / ".cache" / f"{face}.tif"
            if not _complete(out, face, settings["level"]):
                log(f"Reprojecting face {face}")
                with rasterio.open(source) as src:
                    _prepare_face(src, cached, face, bands, settings)
                    with rasterio.open(cached) as cache:
                        for level, x, y in pyramid(settings["level"]):
                            check()
                            path = _tile_path(out, face, level, x, y)
                            if not path.exists():
                                _extract(cache, path, level, x, y, bands, settings)
            if not settings["keep"]:
                cached.unlink(missing_ok=True)
            log(f"Face {face} ready")
        except Exception as exc:
            stop.set()
            with lock:
                if not errors:
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(run_face, range(6)))
    if errors:
        stop.clear()  # It only stopped this build's faces.
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
        generation={"strategy": "cached", "pngCompression": png_compression},
        toolchain={"pipelineVersion": PIPELINE_VERSION, "gdal": gdal_version()},
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


def _from_dataset_record(old):
    """Version-3 records embedded the whole dataset description."""
    if old.get("pipelineVersion") != PIPELINE_VERSION or "dataset" not in old:
        return None
    upgraded = pick(old, ("pipelineVersion", "gdal", "sourceSha256"))
    upgraded["radiusMeters"] = float(old["dataset"].get("radiusMeters") or 0)
    upgraded.update(pick(old, ("tileSize", "gutter", "maxLevel", "pngCompression")))
    return upgraded


def _from_v2(out):
    """Version-2 pyramids are pixel-compatible, but only accepted when finished."""

    def upgrade(old):
        if old.get("pipelineVersion") != 2 or not (out / "manifest.json").exists():
            return None
        return {
            "pipelineVersion": PIPELINE_VERSION,
            "gdal": gdal_version(),
            "sourceSha256": old.get("sourceSha256"),
            "radiusMeters": float(old.get("dataset", {}).get("radiusMeters") or 0),
            "tileSize": old.get("tileSize"),
            "gutter": old.get("gutter"),
            "maxLevel": old.get("maxLevel"),
            "pngCompression": old.get("pngCompression", 3),
        }

    return upgrade
