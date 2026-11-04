"""Global elevation grids: one equirectangular uint16 raster per body.

A grid directory holds `heights.u16` and `manifest.json`. The height pyramid
(`tile_service.heights`) samples it. Rebuilding an up-to-date grid is a no-op.
"""

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from tile_service.cube import (
    HEIGHT_MAX,
    HEIGHT_MIN,
    HEIGHT_OFFSET,
    HEIGHT_SCALE,
    encode_heights,
    equirect_affine,
    equirect_crs,
    height_range,
)
from tile_service.runtime import atomic_json, atomic_write, log, read_json, require_sphere, sha256

NUMERIC = ("float32", "float64", "int16", "int32", "uint16")


def current(out, source, width, height):
    """True when `out` already holds this grid; refuses to overwrite anything else."""
    out = Path(out)
    manifest = out / "manifest.json"
    if manifest.exists():
        old = read_json(manifest)
        if (
            old.get("sourceSha256") == sha256(source)
            and old.get("width") == width
            and old.get("height") == height
        ):
            log(f"Elevation grid up to date: {out}")
            return True
        raise RuntimeError(f"Elevation grid was built from another source/size: {out}")
    if out.exists() and any(out.iterdir()):
        raise RuntimeError("Prepare elevation into a new or empty directory")
    return False


def _level_poles(encoded):
    # One cylindrical row is the whole pole, so every longitude stores the same height.
    for row in (0, encoded.shape[0] - 1):
        encoded[row, :] = int(np.rint(encoded[row].astype(np.float64).mean()))
    return height_range(encoded)


def warp(
    source,
    out,
    *,
    body,
    radius,
    width=4096,
    height=2048,
    input_scale=1,
    input_offset=0,
    nodata_height=0,
    credit="",
    source_page="",
    geographic=False,
):
    """Resample a one-band DEM onto the body's equirectangular grid.

    The source must use the body sphere, or with `geographic` any
    longitude/latitude CRS; geographic samples beyond the encoding are clamped.
    """
    source, out = Path(source), Path(out)
    recorded_radius, radius = radius, float(radius)
    scale, offset, missing = float(input_scale), float(input_offset), float(nodata_height)
    if not (
        body
        and math.isfinite(radius)
        and radius > 0
        and math.isfinite(scale)
        and scale > 0
        and math.isfinite(offset)
        and math.isfinite(missing)
    ):
        raise RuntimeError("Invalid elevation source/body/units")
    if not 2 <= width <= 16384 or not 2 <= height <= 16384:
        raise RuntimeError("Elevation dimensions must be 2..16384")
    if current(out, source, width, height):
        return read_json(out / "manifest.json")
    grid = np.zeros((2, height, width), np.float64)
    with rasterio.open(source) as src:
        if src.count != 1 or src.dtypes[0] not in NUMERIC:
            raise RuntimeError("Elevation source must have one numeric band")
        options = {}
        if geographic:
            # EPSG:9518 includes an EGM2008 axis. Warp longitude and latitude only.
            if src.crs is None or not src.crs.is_geographic or src.crs.geodetic_crs is None:
                raise RuntimeError("Elevation source must be a geographic longitude/latitude grid")
            options["src_crs"] = src.crs.geodetic_crs
        else:
            require_sphere(src.crs, radius, "Source CRS must match the configured reference sphere")
        reproject(
            source=rasterio.band(src, [1]),
            destination=grid,
            src_nodata=src.nodata,
            dst_transform=equirect_affine(radius, width, height),
            dst_crs=equirect_crs(radius),
            resampling=Resampling.bilinear,
            dst_alpha=2,
            num_threads=1,
            warp_mem_limit=128,
            **options,
        )
    values = grid[0]
    usable = (grid[1] > 0) & np.isfinite(values)
    meters = values * scale + offset
    if geographic:
        outside = usable & ((meters < HEIGHT_MIN) | (meters > HEIGHT_MAX))
        # shortcut: trenches below -10000 m are stored as -10000 m. Raise the shared encoding before measuring deep ocean.
        meters = np.clip(meters, HEIGHT_MIN, HEIGHT_MAX)
        log(f"Clamped {int(np.count_nonzero(outside))} samples into [{HEIGHT_MIN}, {HEIGHT_MAX}] m")
    encoded = encode_heights(np.where(usable, meters, missing))
    low, high = _level_poles(encoded)
    out.mkdir(parents=True, exist_ok=True)
    raw = encoded.astype("<u2").tobytes()
    atomic_write(out / "heights.u16", raw)
    manifest = {
        "version": 1,
        "format": "uint16-le",
        "rowOrder": "north-to-south",
        "longitude": "-180-to-180-east",
        "pixelRegistration": "centers",
        "scaleMeters": HEIGHT_SCALE,
        "offsetMeters": HEIGHT_OFFSET,
        "data": "heights.u16",
        "body": body,
        "radiusMeters": recorded_radius,
        "width": width,
        "height": height,
        "bytes": len(raw),
        "minMeters": low,
        "maxMeters": high,
        "dataSha256": sha256(out / "heights.u16"),
        "sourceSha256": sha256(source),
        "credit": credit,
        "sourcePage": source_page,
        "nodataHeightMeters": missing,
    }
    atomic_json(out / "manifest.json", manifest)
    log(f"Prepared {body} elevation grid: {out}")
    return manifest


def lola(source, out, entry):
    """Copy NASA's LOLA uint16 map, which already uses the shared encoding."""
    source, out = Path(source), Path(out)
    width, height = 5760, 2880
    if current(out, source, width, height):
        return read_json(out / "manifest.json")
    with rasterio.open(source) as dataset:
        if (
            dataset.width != width
            or dataset.height != height
            or dataset.count != 1
            or dataset.dtypes[0] != "uint16"
        ):
            raise RuntimeError("Expected NASA 5760x2880 uint16 LOLA map")
        values = dataset.read(1)
    for row in (0, height - 1):
        values[row, :] = int(np.rint(values[row].astype(np.float64).sum() / width))
    low, high = height_range(values)
    raw = np.ascontiguousarray(values.astype("<u2")).tobytes()
    atomic_write(out / "heights.u16", raw)
    manifest = {
        "version": 1,
        "body": "Moon",
        "radiusMeters": entry["radiusMeters"],
        "width": width,
        "height": height,
        "format": "uint16-le",
        "rowOrder": "north-to-south",
        "longitude": "-180-to-180-east",
        "pixelRegistration": "centers",
        "scaleMeters": HEIGHT_SCALE,
        "offsetMeters": HEIGHT_OFFSET,
        "data": "heights.u16",
        "bytes": len(raw),
        "sourcePage": entry["sourcePage"],
        "polarRows": "longitude mean",
        "credit": entry["credit"],
        "minMeters": low,
        "maxMeters": high,
        "sourceURL": entry["sourceUrl"],
        "sourceSha256": sha256(source),
        "dataSha256": sha256(out / "heights.u16"),
        "nominalSampleSpacingMeters": 2 * math.pi * entry["radiusMeters"] / width,
    }
    atomic_json(out / "manifest.json", manifest)
    log(f"Prepared numeric LOLA elevation: {out}")
    return manifest
