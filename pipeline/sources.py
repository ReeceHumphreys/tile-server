import ctypes
import math
import os
import shutil
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import Affine

from pipeline.runtime import (
    DEM_SHA,
    DEM_URL,
    EARTH_RADIUS,
    MOON_RADIUS,
    PI,
    Args,
    atomic_json,
    atomic_write,
    config,
    gdal_library,
    log,
    read_json,
    require_sphere,
    run,
    sha256,
    sphere_crs,
    validate_source,
)

# Ice surface, one global 60 arc-second GeoTIFF. Verified 2026-10-18: 465,969,062 bytes.
ETOPO_URL = (
    "https://www.ngdc.noaa.gov/mgg/global/relief/ETOPO2022/data/60s/"
    "60s_surface_elev_gtif/ETOPO_2022_v1_60s_N90W180_surface.tif"
)
ETOPO_SOURCE = "data/raw/earth/ETOPO_2022_v1_60s_N90W180_surface.tif"
ETOPO_BYTES = 465_969_062
ETOPO_SHA = "9d27d4b8ea8e76977e2988bca667d7c8fa68b927355feffcddd6b4875a7fd08e"
ETOPO_OUTPUT = "public/data/earth/elevation"
ETOPO_PAGE = "https://www.ncei.noaa.gov/products/etopo-global-relief-model"
ETOPO_CREDIT = (
    "NOAA National Centers for Environmental Information. 2022: ETOPO 2022 "
    "15 Arc-Second Global Relief Model, 60 arc-second ice surface. "
    "DOI: 10.25921/fd45-gt74. Samples outside -10000 to 22767.5 m are clamped."
)


def _tiff(path):
    with open(path, "rb") as handle:
        magic = handle.read(4)
    return magic in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def download(url, partial, etag="", last_modified=""):
    Path(partial).parent.mkdir(parents=True, exist_ok=True)
    args = [
        "curl",
        "--fail",
        "--location",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--continue-at",
        "-",
        "--output",
        str(partial),
    ]
    if etag.startswith("W/"):
        if not last_modified:
            raise RuntimeError("Weak ETag requires sourceLastModified for safe resumption")
        args += ["--header", "If-Unmodified-Since: " + last_modified]
    elif etag:
        args += ["--header", "If-Match: " + etag]
    args.append(url)
    run(args)


def fetch(args: Args):
    args.validate({"--config", "--output", "--info"})
    source = config(args)
    out = Path(args.get("--output", source.get("sourceFile", "")))
    log(
        f"{source.get('title', '')}\n{source.get('sourceSizeNote', '')}\n"
        f"{source.get('sourceUrl', '')}\nDestination: {out}"
    )
    if args.has("--info"):
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    receipt_path = Path(str(out) + ".receipt.json")
    if out.exists():
        if not receipt_path.exists():
            raise RuntimeError("Existing source has no receipt; choose another --output.")
        old = read_json(receipt_path)
        if (
            old.get("url") != source.get("sourceUrl")
            or old.get("etag") != source.get("sourceETag")
            or old.get("sha256") != sha256(out)
        ):
            raise RuntimeError(
                "Existing source does not match its download receipt. No file overwritten."
            )
        log("Verified existing source; no download needed.")
        return
    part = Path(str(out) + ".part")
    expected = int(source["expectedBytes"])
    have = part.stat().st_size if part.exists() else 0
    free = shutil.disk_usage(out.parent).free
    if free < (expected - have if expected > have else 0) + 16 * 1024 * 1024:
        raise RuntimeError("Insufficient disk space for source download.")
    if not part.exists() or part.stat().st_size != expected:
        download(
            source["sourceUrl"],
            part,
            source.get("sourceETag") or "",
            source.get("sourceLastModified") or "",
        )
    if part.stat().st_size != expected:
        raise RuntimeError("Incomplete download; rerun to resume.")
    if not _tiff(part):
        raise RuntimeError("Download is not TIFF; partial file retained for inspection.")
    # Receipt first: a crash before the final rename stays resumable.
    atomic_json(
        receipt_path,
        {
            "url": source.get("sourceUrl"),
            "etag": source.get("sourceETag"),
            "sourcePage": source.get("sourcePage"),
            "bytes": expected,
            "sha256": sha256(part),
            "checksumNote": "Locally computed integrity receipt, not a publisher-provided checksum.",
        },
    )
    os.replace(part, out)
    log(f"Complete: {out}")


def _fill_fixture(constant, nodata, height):
    bands = 1 if height else 3
    pixels = np.empty((bands, 360, 720), np.float32 if height else np.uint8)
    for y in range(360):
        for x in range(720):
            lon = (x * 0.5 - 179.75) * PI / 180
            lat = (89.75 - y * 0.5) * PI / 180
            raw = (
                math.cos(lat) * math.sin(lon),
                math.sin(lat),
                math.cos(lat) * math.cos(lon),
            )
            for band in range(bands):
                if nodata and y < 180 and x >= 360:
                    value = 0
                elif constant is not None:
                    value = constant
                else:
                    value = int(np.rint(127 + 100 * raw[band]))
                pixels[band, y, x] = value
    return pixels


def fixture(args: Args):
    args.validate(
        {
            "--output",
            "--earth",
            "--nodata",
            "--constant",
            "--height",
            "--polar-quadrant",
            "--body-radius",
        }
    )
    constant = args.integer("--constant", -1) if args.has("--constant") else None
    if constant is not None and not 0 <= constant <= 255:
        raise RuntimeError("Fixture constant must be 0..255")
    radius = float(args.get("--body-radius", "6378137" if args.has("--earth") else "1737400"))
    if not math.isfinite(radius) or radius <= 0:
        raise RuntimeError("Invalid fixture radius")
    output = args.get("--output")
    if not output:
        raise RuntimeError("Fixture requires --output")
    height = args.has("--height")
    pixels = _fill_fixture(constant, args.has("--nodata"), height)
    if args.has("--polar-quadrant"):
        quadrant = args.integer("--polar-quadrant", 0)
        if not 0 <= quadrant <= 3:
            raise RuntimeError("Polar quadrant must be 0..3")
        left = 0 if quadrant < 2 else -30000
        top = 30000 if quadrant in (0, 3) else 0
        crs = sphere_crs(f"+proj=stere +lat_0=-90 +lat_ts=-90 +lon_0=0 +R={radius}")
        transform = Affine(30000 / 720, 0, left, 0, -30000 / 360, top)
    else:
        crs = sphere_crs(f"+proj=eqc +R={radius}")
        transform = Affine(
            2 * PI * radius / 720, 0, -PI * radius, 0, -PI * radius / 360, PI * radius / 2
        )
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        output,
        "w",
        driver="GTiff",
        width=720,
        height=360,
        count=pixels.shape[0],
        dtype=pixels.dtype,
        crs=crs,
        transform=transform,
        nodata=0 if args.has("--nodata") else None,
    ) as dataset:
        dataset.write(pixels)


def inspect(args: Args):
    args.validate({"--source", "--output"})
    with rasterio.open(args.get("--source")) as dataset:
        planes = dataset.read(out_dtype="uint8")
    interleaved = np.ascontiguousarray(np.moveaxis(planes, 0, -1))
    atomic_write(args.get("--output"), interleaved.tobytes())


def _build_vrt(paths, dest):
    library = ctypes.CDLL(str(gdal_library()))
    library.GDALAllRegister.restype = None
    library.GDALAllRegister()
    library.GDALBuildVRTOptionsNew.restype = ctypes.c_void_p
    library.GDALBuildVRTOptionsNew.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_void_p]
    library.GDALBuildVRTOptionsFree.argtypes = [ctypes.c_void_p]
    library.GDALBuildVRT.restype = ctypes.c_void_p
    library.GDALBuildVRT.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    library.GDALClose.argtypes = [ctypes.c_void_p]
    library.GDALClose.restype = ctypes.c_int
    argv = (ctypes.c_char_p * 2)(b"-strict", None)
    options = library.GDALBuildVRTOptionsNew(argv, None)
    if not options:
        raise RuntimeError("Cannot configure mosaic")
    encoded = [str(path).encode() for path in paths]
    names = (ctypes.c_char_p * len(encoded))(*encoded)
    error = ctypes.c_int(0)
    result = library.GDALBuildVRT(
        str(dest).encode(),
        len(encoded),
        None,
        names,
        options,
        ctypes.byref(error),
    )
    library.GDALBuildVRTOptionsFree(options)
    if not result or error.value:
        raise RuntimeError("Cannot build lunar mosaic")
    if library.GDALClose(result) != 0:
        raise RuntimeError("GDAL raster close failed")


def mosaic(args: Args):
    args.validate({"--sources", "--output"})
    files = read_json(args.get("--sources"))
    if not isinstance(files, list) or not 1 <= len(files) <= 40:
        raise RuntimeError("Mosaic requires 1..40 sources")
    paths = []
    for file in files:
        path = Path(file).resolve()
        with rasterio.open(path) as source:
            validate_source(source, MOON_RADIUS)
        paths.append(path)
    out = Path(args.get("--output"))
    if not str(out):
        raise RuntimeError("Mosaic requires --output")
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(out) + ".tmp")
    _build_vrt(paths, temp)
    if out.exists() and out.read_bytes() == temp.read_bytes():
        temp.unlink()
    else:
        os.replace(temp, out)
    log(f"Virtual mosaic ready: {out}")


def _level_poles(encoded):
    # One cylindrical row is the whole pole, so every longitude stores the same height.
    for row in (0, encoded.shape[0] - 1):
        encoded[row, :] = int(np.rint(encoded[row].astype(np.float64).mean()))
    decoded = encoded.astype(np.float64) * 0.5 - 10000
    return float(decoded.min()), float(decoded.max())


def _quantize(samples, scale, offset, missing):
    alpha = samples[1]
    values = samples[0]
    usable = (alpha > 0) & np.isfinite(values)
    heights = np.where(usable, values * scale + offset, missing)
    if not np.isfinite(heights).all() or heights.min() < -10000 or heights.max() > 22767.5:
        raise RuntimeError("Elevation outside uint16 encoding range [-10000, 22767.5] meters")
    encoded = np.rint((heights + 10000) * 2).astype(np.uint16)
    decoded = encoded.astype(np.float64) * 0.5 - 10000
    return encoded, float(decoded.min()), float(decoded.max())


def _store_elevation(
    out, body, radius, width, height, encoded, low, high, source, credit, page, missing
):
    out.mkdir(parents=True, exist_ok=True)
    raw = encoded.astype("<u2").tobytes()
    atomic_write(out / "heights.u16", raw)
    atomic_json(
        out / "manifest.json",
        {
            "version": 1,
            "format": "uint16-le",
            "rowOrder": "north-to-south",
            "longitude": "-180-to-180-east",
            "pixelRegistration": "centers",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "data": "heights.u16",
            "body": body,
            "radiusMeters": radius,
            "width": width,
            "height": height,
            "bytes": len(raw),
            "minMeters": low,
            "maxMeters": high,
            "dataSha256": sha256(out / "heights.u16"),
            "sourceSha256": sha256(source),
            "credit": credit,
            "sourcePage": page,
            "nodataHeightMeters": missing,
        },
    )
    log(f"Prepared {body} elevation grid")


def prepare_elevation(args: Args):
    args.validate(
        {
            "--source",
            "--output",
            "--body",
            "--body-radius",
            "--width",
            "--height",
            "--input-scale",
            "--input-offset",
            "--nodata-height",
            "--credit",
            "--source-page",
        }
    )
    source = Path(args.get("--source"))
    out = Path(args.get("--output"))
    body = args.get("--body")
    radius = float(args.get("--body-radius"))
    scale = float(args.get("--input-scale", "1"))
    offset = float(args.get("--input-offset", "0"))
    missing = float(args.get("--nodata-height", "0"))
    width = args.integer("--width", 4096)
    height = args.integer("--height", 2048)
    if (
        not source
        or not out
        or not body
        or not math.isfinite(radius)
        or radius <= 0
        or not math.isfinite(scale)
        or scale <= 0
        or not math.isfinite(offset)
        or not math.isfinite(missing)
    ):
        raise RuntimeError("Invalid elevation source/body/units")
    if not 2 <= width <= 16384 or not 2 <= height <= 16384:
        raise RuntimeError("Elevation dimensions must be 2..16384")
    if out.exists() and any(out.iterdir()):
        raise RuntimeError("Prepare elevation into a new or empty directory")
    crs = sphere_crs(f"+proj=eqc +R={radius}")
    transform = Affine(
        2 * PI * radius / width,
        0,
        -PI * radius,
        0,
        -PI * radius / height,
        PI * radius / 2,
    )
    with rasterio.open(source) as src:
        if src.count != 1:
            raise RuntimeError("Elevation source must have one numeric band")
        require_sphere(src.crs, radius, "Source CRS must match the configured reference sphere")
        grid = np.zeros((2, height, width), np.float64)
        from rasterio.warp import reproject

        reproject(
            source=rasterio.band(src, [1]),
            destination=grid,
            src_nodata=src.nodata,
            dst_transform=transform,
            dst_crs=crs,
            resampling=Resampling.bilinear,
            dst_alpha=2,
            num_threads=1,
            warp_mem_limit=128,
        )
    encoded, _, _ = _quantize(grid, scale, offset, missing)
    low, high = _level_poles(encoded)
    _store_elevation(
        out,
        body,
        radius,
        width,
        height,
        encoded,
        low,
        high,
        source,
        args.get("--credit", ""),
        args.get("--source-page", ""),
        missing,
    )


def elevation(args: Args):
    args.validate({"--source", "--output"})
    source = Path(args.get("--source", "data/raw/moon/ldem_16_uint.tif"))
    out = Path(args.get("--output", "public/data/moon/elevation"))
    if source.exists() and sha256(source) != DEM_SHA:
        raise RuntimeError(
            "Source checksum does not match the pinned NASA LOLA file. Remove it to download again."
        )
    if not source.exists():
        partial = Path(str(source) + ".part")
        download(DEM_URL, partial)
        if sha256(partial) != DEM_SHA:
            raise RuntimeError(
                "NASA elevation source changed; review before updating pinned checksum"
            )
        os.replace(partial, source)
    with rasterio.open(source) as dataset:
        if (
            dataset.width != 5760
            or dataset.height != 2880
            or dataset.count != 1
            or dataset.dtypes[0] != "uint16"
        ):
            raise RuntimeError("Expected NASA 5760x2880 uint16 LOLA map")
        values = dataset.read(1)
    for row in (0, 2879):
        mean = int(np.rint(values[row].astype(np.float64).sum() / 5760))
        values[row, :] = mean
    low = float(values.min()) * 0.5 - 10000
    high = float(values.max()) * 0.5 - 10000
    raw = np.ascontiguousarray(values.astype("<u2")).tobytes()
    atomic_write(out / "heights.u16", raw)
    atomic_json(
        out / "manifest.json",
        {
            "version": 1,
            "body": "Moon",
            "radiusMeters": MOON_RADIUS,
            "width": 5760,
            "height": 2880,
            "format": "uint16-le",
            "rowOrder": "north-to-south",
            "longitude": "-180-to-180-east",
            "pixelRegistration": "centers",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "data": "heights.u16",
            "bytes": 33177600,
            "sourcePage": "https://svs.gsfc.nasa.gov/4720/",
            "polarRows": "longitude mean",
            "credit": "NASA SVS / LRO / LOLA; Ernie Wright",
            "minMeters": low,
            "maxMeters": high,
            "sourceURL": DEM_URL,
            "sourceSha256": DEM_SHA,
            "dataSha256": sha256(out / "heights.u16"),
            "nominalSampleSpacingMeters": 2 * PI * MOON_RADIUS / 5760,
        },
    )
    log(f"Prepared numeric LOLA elevation: {out}")


def _ensure_etopo(source):
    official = Path(ETOPO_SOURCE).resolve()
    receipt_path = Path(str(source) + ".receipt.json")
    if source.exists():
        if source.resolve() != official:
            return
        if not receipt_path.exists():
            raise RuntimeError("Existing ETOPO source has no receipt; choose another --source.")
        old = read_json(receipt_path)
        if (
            old.get("url") != ETOPO_URL
            or old.get("bytes") != ETOPO_BYTES
            or old.get("sha256") != ETOPO_SHA
            or old.get("sha256") != sha256(source)
        ):
            raise RuntimeError(
                "Existing ETOPO source does not match its receipt. Choose another --source."
            )
        return
    if source.resolve() != official:
        raise RuntimeError("Elevation source not found")
    source.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(source.parent).free
    if free < ETOPO_BYTES + 64 * 1024 * 1024:
        raise RuntimeError("Insufficient disk space for the ETOPO source.")
    partial = Path(str(source) + ".part")
    if not partial.exists() or partial.stat().st_size != ETOPO_BYTES:
        download(ETOPO_URL, partial)
    if partial.stat().st_size != ETOPO_BYTES or not _tiff(partial):
        raise RuntimeError("ETOPO source size changed; review before updating the pinned size")
    digest = sha256(partial)
    if digest != ETOPO_SHA:
        raise RuntimeError(
            "ETOPO source checksum changed; review before updating the pinned checksum"
        )
    # Receipt first: a crash before the rename stays resumable and does not look complete.
    atomic_json(
        receipt_path,
        {"url": ETOPO_URL, "bytes": ETOPO_BYTES, "sha256": digest},
    )
    os.replace(partial, source)


def _warp_geographic(src, width, height):
    # EPSG:9518 includes an EGM2008 axis. Warp longitude and latitude only; the band is already meters.
    horizontal = src.crs.geodetic_crs
    if horizontal is None:
        raise RuntimeError("Earth elevation source must be a geographic longitude/latitude grid")
    transform = Affine(
        2 * PI * EARTH_RADIUS / width,
        0,
        -PI * EARTH_RADIUS,
        0,
        -PI * EARTH_RADIUS / height,
        PI * EARTH_RADIUS / 2,
    )
    grid = np.zeros((2, height, width), np.float64)
    from rasterio.warp import reproject

    reproject(
        source=rasterio.band(src, [1]),
        destination=grid,
        src_crs=horizontal,
        dst_transform=transform,
        dst_crs=sphere_crs(f"+proj=eqc +R={EARTH_RADIUS}"),
        resampling=Resampling.bilinear,
        src_nodata=src.nodata,
        dst_alpha=2,
        num_threads=1,
        warp_mem_limit=128,
    )
    return grid


def earth_elevation(args: Args):
    args.validate({"--source", "--output", "--width", "--height", "--info"})
    source = Path(args.get("--source", ETOPO_SOURCE))
    out = Path(args.get("--output", ETOPO_OUTPUT))
    # shortcut: 16384 samples is the shared grid cap, about 2.4 km on Earth. The browser global grid stops at 8192, so the viewer loads the tiles.
    width = args.integer("--width", 16384)
    height = args.integer("--height", 8192)
    if not 2 <= width <= 16384 or not 2 <= height <= 16384:
        raise RuntimeError("Elevation dimensions must be 2..16384")
    if args.has("--info"):
        log(
            f"{ETOPO_CREDIT}\n{ETOPO_URL}\n{ETOPO_BYTES:,} bytes\n"
            f"Source: {source}\nGrid: {width} x {height}\nOutput: {out}"
        )
        return
    if out.exists() and any(out.iterdir()):
        raise RuntimeError("Prepare elevation into a new or empty directory")
    _ensure_etopo(source)
    with rasterio.open(source) as src:
        if src.count != 1 or src.dtypes[0] not in (
            "float32",
            "float64",
            "int16",
            "int32",
            "uint16",
        ):
            raise RuntimeError("Earth elevation source must be one numeric band")
        if src.crs is None or not src.crs.is_geographic:
            raise RuntimeError(
                "Earth elevation source must be a geographic longitude/latitude grid"
            )
        grid = _warp_geographic(src, width, height)
    values = grid[0]
    usable = (grid[1] > 0) & np.isfinite(values)
    outside = usable & ((values < -10000) | (values > 22767.5))
    # shortcut: trenches below -10000 m are stored as -10000 m. Raise the shared encoding before measuring deep ocean.
    grid[0] = np.where(usable, np.clip(values, -10000, 22767.5), values)
    log(
        f"Clamped {int(np.count_nonzero(outside))} Earth elevation samples into [-10000, 22767.5] m"
    )
    encoded, _, _ = _quantize(grid, 1, 0, 0)
    low, high = _level_poles(encoded)
    _store_elevation(
        out,
        "Earth",
        EARTH_RADIUS,
        width,
        height,
        encoded,
        low,
        high,
        source,
        ETOPO_CREDIT,
        ETOPO_PAGE,
        0,
    )
