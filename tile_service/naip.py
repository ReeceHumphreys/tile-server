"""USDA NAIP site crops: warp pinned cloud-optimized quads onto Earth's sphere."""

import math
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import reproject

from tile_service.cube import EARTH_RADIUS, equirect_crs
from tile_service.runtime import atomic_json, check, log, read_json, sha256

# Geodetic degrees go onto the sphere as degrees. That is the cloudless mosaic's convention.


def _http():
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
    os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
    os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
    os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "2")


def _grid(latitude, longitude, extent, radius, pixel):
    reach = extent + pixel
    lat = math.radians(latitude)
    lon = math.radians(longitude)
    dlat = reach / radius
    dlon = reach / (radius * math.cos(lat))
    north, south = lat + dlat, lat - dlat
    east, west = lon + dlon, lon - dlon
    width = max(1, math.ceil((east - west) * radius / pixel))
    height = max(1, math.ceil((north - south) * radius / pixel))
    transform = rasterio.transform.Affine(pixel, 0, west * radius, 0, -pixel, north * radius)
    return transform, width, height


def _mosaic(hrefs, transform, width, height, radius):
    acc = np.zeros((3, height, width), np.uint8)
    covered = np.zeros((height, width), dtype=bool)
    dst = equirect_crs(radius)
    for href in hrefs:
        check()
        log(f"Reading {href}")
        frame = np.zeros((4, height, width), np.uint8)
        with rasterio.open(href) as src:
            if src.crs is None or src.count < 3 or any(src.dtypes[i] != "uint8" for i in range(3)):
                raise RuntimeError("NAIP source must be a georeferenced uint8 RGB raster")
            reproject(
                source=rasterio.band(src, [1, 2, 3]),
                destination=frame,
                dst_transform=transform,
                dst_crs=dst,
                resampling=Resampling.bilinear,
                dst_alpha=4,
                num_threads=1,
                warp_mem_limit=128,
            )
        fresh = (frame[3] > 0) & ~covered
        acc[:, fresh] = frame[:3, fresh]
        covered |= frame[3] > 0
        log(f"Warped {int((frame[3] > 0).sum())} pixels from {href}")
        del frame
    if not covered.any():
        raise RuntimeError("NAIP windows do not cover this site")
    return acc, covered


def _write_crop(path, rgb, covered, transform, radius):
    # shortcut: exact black is the nodata sentinel, so a covered black pixel is stored as 1,0,0.
    black = covered & (rgb[0] == 0) & (rgb[1] == 0) & (rgb[2] == 0)
    rgb = rgb.copy()
    rgb[0, black] = 1
    rgb[:, ~covered] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = Path(str(path) + ".partial")
    with rasterio.open(
        partial,
        "w",
        driver="GTiff",
        width=rgb.shape[2],
        height=rgb.shape[1],
        count=3,
        dtype="uint8",
        crs=equirect_crs(radius),
        transform=transform,
        nodata=0,
        compress="deflate",
        predictor=2,
        tiled=True,
        blockxsize=512,
        blockysize=512,
    ) as dataset:
        dataset.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
        dataset.write(rgb)
    os.replace(partial, path)


def covers(site, extent):
    """True when the pinned quads cover the whole site disk."""
    latitude, longitude = site["latitudeDeg"], site["longitudeDeg"]
    sources = site["sources"]
    dlat = math.degrees(extent / EARTH_RADIUS)
    dlon = dlat / math.cos(math.radians(latitude))
    west, east = longitude - dlon, longitude + dlon
    south, north = latitude - dlat, latitude + dlat
    for i in range(9):
        for j in range(9):
            x = west + (east - west) * i / 8
            y = south + (north - south) * j / 8
            if not any(
                box[0] <= x <= box[2] and box[1] <= y <= box[3]
                for box in (source["bbox"] for source in sources)
            ):
                return False
    return True


def crop(site, extent, max_level, folder):
    """A cached GeoTIFF of the site disk on the sphere, with a receipt."""
    latitude, longitude = float(site["latitudeDeg"]), float(site["longitudeDeg"])
    pixel = float(site["nativeResolutionMeters"])
    hrefs = [source["href"] for source in site["sources"]]
    identity = {
        "hrefs": hrefs,
        "latitudeDeg": latitude,
        "longitudeDeg": longitude,
        "radiusMeters": extent,
        "pixelMeters": pixel,
        "bodyRadiusMeters": EARTH_RADIUS,
    }
    # The filename includes depth so a coarser rerun cannot replace the level-16 source.
    path = Path(folder) / f"{site['id']}-r{int(extent)}-z{max_level}.tif"
    receipt = Path(str(path) + ".receipt.json")
    if path.exists() and receipt.exists():
        old = read_json(receipt)
        digest = old.pop("sha256", None)
        if old == identity and digest == sha256(path):
            log(f"Reusing {path}")
            return path
    _http()
    grid, width, height = _grid(latitude, longitude, extent, EARTH_RADIUS, pixel)
    rgb, covered = _mosaic(hrefs, grid, width, height, EARTH_RADIUS)
    _write_crop(path, rgb, covered, grid, EARTH_RADIUS)
    atomic_json(receipt, {**identity, "sha256": sha256(path)})
    log(f"Wrote {path}")
    return path
