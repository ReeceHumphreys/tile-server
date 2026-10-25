import math
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import reproject, transform

from pipeline.region import disk_addresses, region
from pipeline.runtime import (
    EARTH_RADIUS,
    PI,
    Args,
    atomic_json,
    catalog_path,
    check,
    log,
    read_json,
    sha256,
    sphere_crs,
)

# Geodetic degrees go onto the sphere as degrees. That is the cloudless mosaic's convention.
CATALOG = catalog_path("earth-sites.json")
DEFAULT_BASE = Path("public/data/earth/s2cloudless-2020-z7/manifest.json")
DEFAULT_OUTPUT = Path("public/data/earth/sites")
RAW = Path("data/raw/earth/sites")


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
    dst = sphere_crs(f"+proj=eqc +R={radius}")
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
        crs=sphere_crs(f"+proj=eqc +R={radius}"),
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


def _covered(latitude, longitude, extent, sources):
    dlat = extent / EARTH_RADIUS * 180 / PI
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


def _crop(site, latitude, longitude, extent, pixel):
    hrefs = [source["href"] for source in site["sources"]]
    identity = {
        "hrefs": hrefs,
        "latitudeDeg": latitude,
        "longitudeDeg": longitude,
        "radiusMeters": extent,
        "pixelMeters": pixel,
        "bodyRadiusMeters": EARTH_RADIUS,
    }
    path = RAW / f"{site['id']}-r{int(extent)}-z{site['maxLevel']}.tif"
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


def _self_test():
    lon, lat = -80.604333, 28.608389
    pixel = 1.0
    xs, ys = transform(CRS.from_epsg(4269), CRS.from_epsg(26917), [lon], [lat])
    n = 32
    col = n // 2
    src_transform = rasterio.transform.Affine(
        pixel,
        0,
        xs[0] - (col + 0.5) * pixel,
        0,
        -pixel,
        ys[0] + (col + 0.5) * pixel,
    )
    image = np.full((3, n, n), 30, np.uint8)
    image[:, col, col] = 220
    path = RAW / "self-test.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=n,
        height=n,
        count=3,
        dtype="uint8",
        crs=CRS.from_epsg(26917),
        transform=src_transform,
    ) as dataset:
        dataset.write(image)
    extent = 12.0
    grid, width, height = _grid(lat, lon, extent, EARTH_RADIUS, pixel)
    acc, _covered_pixels = _mosaic([path], grid, width, height, EARTH_RADIUS)
    path.unlink()
    expected_x = math.radians(lon) * EARTH_RADIUS
    expected_y = math.radians(lat) * EARTH_RADIUS
    expected_col = (expected_x - grid.c) / grid.a - 0.5
    expected_row = (expected_y - grid.f) / grid.e - 0.5
    row, column = np.unravel_index(int(np.argmax(acc[0])), acc[0].shape)
    shift = math.hypot((column - expected_col) * pixel, (row - expected_row) * pixel)
    if shift >= 1.5 or int(acc[0, row, column]) < 100:
        raise RuntimeError(f"geodetic shift {shift:.3f} m")
    log(f"geodetic shift {shift:.3f} m")


def earth_sites(args: Args):
    args.validate(
        {"--site", "--info", "--self-test", "--radius-meters", "--max-level", "--output", "--base"}
    )
    if args.has("--self-test"):
        _self_test()
        return
    catalog = read_json(CATALOG)
    selected = args.get("--site").lower().replace(" ", "")
    if not selected:
        log(
            "Usage: tile-service earth-sites --site starbase|ksc|all [--info] "
            "[--radius-meters 2000] [--max-level 16]"
        )
        for site in catalog["sites"]:
            log(
                f"{site['id']}: {site['name']}; {site['nativeResolutionMeters']} m/px; "
                f"{len(site['sources'])} NAIP quads"
            )
        return
    sites = (
        catalog["sites"]
        if selected == "all"
        else [s for s in catalog["sites"] if s["id"] == selected]
    )
    if not sites:
        raise RuntimeError("Unknown Earth site; run without --site to list them.")
    base = Path(args.get("--base", str(DEFAULT_BASE)))
    output = Path(args.get("--output", str(DEFAULT_OUTPUT)))
    manifest = read_json(base)
    base_level = int(manifest["maxLevel"])
    radius = float(manifest["radiusMeters"])
    for site in sites:
        check()
        extent = float(args.integer("--radius-meters", int(site["radiusMeters"])))
        max_level = args.integer("--max-level", int(site["maxLevel"]))
        latitude = float(site["latitudeDeg"])
        longitude = float(site["longitudeDeg"])
        _center, addresses = disk_addresses(
            latitude, longitude, extent, radius, base_level, max_level
        )
        if len(addresses) > 20000:
            raise RuntimeError("Region exceeds 20,000 tile safety limit; reduce radius or depth")
        if not _covered(latitude, longitude, extent, site["sources"]):
            raise RuntimeError("Pinned NAIP quads do not cover this disk; reduce the radius")
        log(
            f"{site['name']}: {latitude}°, {longitude}°; radius {extent:.0f} m; "
            f"levels {base_level + 1}..{max_level}; {len(addresses)} tiles"
        )
        quads = len(site["sources"])
        log(f"{site['detail']} {site['product']}; {quads} quad" + ("s" if quads != 1 else ""))
        log(f"Output: {output / site['id']}")
        log(f"Base: {base}")
        if args.has("--info"):
            continue
        # The crop filename includes depth so a coarser rerun cannot replace the level-16 source.
        site = {**site, "maxLevel": max_level}
        pixel = float(site["nativeResolutionMeters"])
        crop = _crop(site, latitude, longitude, extent, pixel)
        config_path = Path(str(crop) + ".source.json")
        atomic_json(
            config_path,
            {
                "id": site["id"],
                "name": site["name"],
                "title": site["name"] + " NAIP",
                "product": site["product"],
                "sourceUrl": site["sources"][0]["href"],
                "sourcePage": site["sourcePage"],
                "credit": site["credit"],
                "nativeResolutionMeters": pixel,
                "sourceFile": str(crop.resolve()),
                "components": [{"product": source["id"]} for source in site["sources"]],
            },
        )
        region(
            Args(
                [
                    "lunar-data",
                    "region",
                    "--config",
                    str(config_path),
                    "--source",
                    str(crop),
                    "--base",
                    str(base),
                    "--output",
                    str(output),
                    "--latitude",
                    str(latitude),
                    "--longitude",
                    str(longitude),
                    "--radius-meters",
                    str(int(extent)),
                    "--max-level",
                    str(max_level),
                ]
            )
        )
