import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.windows import Window

from pipeline.runtime import (
    Args,
    atomic_json,
    check,
    config,
    face_crs,
    gdal_version,
    log,
    read_json,
    sha256,
    stop,
    tile_affine,
    validate_source,
    write_png,
)

MANIFEST = {
    "version": 1,
    "body": "Moon",
    "projection": "normalized-cube",
    "faces": [
        {"id": 0, "name": "+X", "normal": [1, 0, 0], "uAxis": [0, 0, -1], "vAxis": [0, 1, 0]},
        {"id": 1, "name": "-X", "normal": [-1, 0, 0], "uAxis": [0, 0, 1], "vAxis": [0, 1, 0]},
        {"id": 2, "name": "+Y", "normal": [0, 1, 0], "uAxis": [1, 0, 0], "vAxis": [0, 0, -1]},
        {"id": 3, "name": "-Y", "normal": [0, -1, 0], "uAxis": [1, 0, 0], "vAxis": [0, 0, 1]},
        {"id": 4, "name": "+Z", "normal": [0, 0, 1], "uAxis": [1, 0, 0], "vAxis": [0, 1, 0]},
        {"id": 5, "name": "-Z", "normal": [0, 0, -1], "uAxis": [-1, 0, 0], "vAxis": [0, 1, 0]},
    ],
    "axes": {"north": "+Y", "longitude0": "+X", "longitude90East": "-Z"},
    "addressing": {
        "origin": "bottom-left in face-local UV",
        "yDirection": "up",
        "template": "{face}/{level}/{x}/{y}.png",
    },
    "imageRowOrder": "top-to-bottom",
    "textureFlipY": True,
    "minLevel": 0,
    "format": "png",
    "channels": "RGBA",
    "noData": "alpha=0; retain coarser imagery underneath",
    "colorSpace": "srgb",
    "resampling": "average",
    "sampler": {
        "minFilter": "linear",
        "magFilter": "linear",
        "generateMipmaps": False,
        "anisotropy": 1,
        "wrapS": "clamp-to-edge",
        "wrapT": "clamp-to-edge",
    },
}


def _tile_path(out, face, level, x, y):
    return out / str(face) / str(level) / str(x) / f"{y}.png"


def _complete(out, face, level):
    for depth in range(level + 1):
        for y in range(1 << depth):
            for x in range(1 << depth):
                if not _tile_path(out, face, depth, x, y).exists():
                    return False
    return True


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
            from rasterio.warp import reproject

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
        from rasterio.warp import reproject

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
    os.replace(temp, path)
    if settings["level"]:
        with rasterio.open(path, "r+") as dest:
            dest.build_overviews(
                [1 << i for i in range(1, settings["level"] + 1)],
                Resampling.average,
            )


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


def tiles(args: Args):
    args.validate(
        {
            "--config",
            "--source",
            "--output",
            "--tile-size",
            "--gutter",
            "--max-level",
            "--strategy",
            "--png-compression",
            "--jobs",
            "--keep-cache",
        }
    )
    source = config(args)
    src_path = args.get("--source", source.get("sourceFile", ""))
    out = Path(args.get("--output", "public/data/moon/wac"))
    settings = {
        "size": args.integer("--tile-size", int(source.get("tileSize") or 0)),
        "gutter": args.integer("--gutter", int(source.get("gutter") or 0)),
        "level": args.integer("--max-level", int(source.get("maxLevel") or 0)),
        "jobs": args.integer("--jobs", 2),
        "compression": args.integer("--png-compression", 3),
        "keep": args.has("--keep-cache"),
        "strategy": args.get("--strategy", "cached"),
        "radius": float(source.get("radiusMeters") or 0),
    }
    size = settings["size"]
    if not (
        16 <= size <= 2048
        and size & (size - 1) == 0
        and 1 <= settings["gutter"] <= 16
        and 0 <= settings["level"] <= 12
    ):
        raise RuntimeError("Use power-of-two tile size 16..2048, gutter 1..16, max level 0..12")
    if not 1 <= settings["jobs"] <= 6:
        raise RuntimeError("Jobs must be 1..6")
    if not 0 <= settings["compression"] <= 9 or settings["strategy"] != "cached":
        raise RuntimeError("Use --strategy cached and PNG compression 0..9")
    if not math.isfinite(settings["radius"]) or settings["radius"] <= 0:
        raise RuntimeError("Invalid body radius")
    with rasterio.open(src_path) as src:
        validate_source(src, settings["radius"])
        bands = src.count
        log("Validating source SHA-256…")
        digest = sha256(src_path)
        metadata = _source_metadata(src, digest, source)
    identity = {
        "pipelineVersion": 3,
        "gdal": gdal_version(),
        "dataset": source,
        "sourceSha256": digest,
        "tileSize": settings["size"],
        "gutter": settings["gutter"],
        "maxLevel": settings["level"],
        "strategy": settings["strategy"],
        "pngCompression": settings["compression"],
    }
    out.mkdir(parents=True, exist_ok=True)
    state = out / "build-state.json"
    legacy = False
    if state.exists():
        old = read_json(state)
        if old != identity:
            legacy = (
                old.get("pipelineVersion") == 2
                and old.get("sourceSha256") == digest
                and old.get("tileSize") == settings["size"]
                and old.get("gutter") == settings["gutter"]
                and old.get("maxLevel") == settings["level"]
                and old.get("dataset") == source
                and (out / "manifest.json").exists()
            )
            if not legacy:
                raise RuntimeError(
                    "Output belongs to another source/settings/toolchain. "
                    "Choose a new --output directory."
                )
    elif any(out.iterdir()):
        raise RuntimeError("Nonempty output without build-state.json; choose a new directory")
    else:
        atomic_json(state, identity)
    if legacy:
        for face in range(6):
            if not _complete(out, face, settings["level"]):
                raise RuntimeError(
                    "Legacy dataset is incomplete; rebuild into a new --output directory."
                )
        log("Verified complete legacy pyramid; retained unchanged.")
        return
    count = 2 * ((1 << (2 * (settings["level"] + 1))) - 1)
    log(f"{count} tiles; {settings['jobs']} face workers.")
    errors = []
    lock = threading.Lock()

    def run_face(face):
        try:
            check()
            cached = out / ".cache" / f"{face}.tif"
            if not _complete(out, face, settings["level"]):
                log(f"Reprojecting face {face}")
                with rasterio.open(src_path) as src:
                    _prepare_face(src, cached, face, bands, settings)
                    with rasterio.open(cached) as cache:
                        for level in range(settings["level"] + 1):
                            for y in range(1 << level):
                                for x in range(1 << level):
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

    with ThreadPoolExecutor(max_workers=settings["jobs"]) as pool:
        list(pool.map(run_face, range(6)))
    if errors:
        raise errors[0]
    check()
    manifest = dict(MANIFEST)
    for key in ("id", "title", "coverage", "nativeResolutionMeters", "quantity"):
        manifest[key] = source.get(key)
    manifest.update(
        {
            "radiusMeters": settings["radius"],
            "body": source.get("body") or "Moon",
            "tileSize": settings["size"],
            "gutter": settings["gutter"],
            "imageSize": settings["size"] + 2 * settings["gutter"],
            "maxLevel": settings["level"],
            "tileCount": count,
            "textureUV": {
                "offset": settings["gutter"] / (settings["size"] + 2 * settings["gutter"]),
                "scale": settings["size"] / (settings["size"] + 2 * settings["gutter"]),
            },
            "faceCenterMetersPerPixelAtMaxLevel": 2
            * settings["radius"]
            / (settings["size"] * (1 << settings["level"])),
            "source": metadata,
            "pipelineVersion": 3,
            "toolchain": {"implementation": "Python", "gdal": gdal_version()},
            "generation": {
                "strategy": settings["strategy"],
                "pngCompression": settings["compression"],
            },
        }
    )
    atomic_json(out / "manifest.json", manifest)
    log(f"Published {out / 'manifest.json'}")
