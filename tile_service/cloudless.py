"""EOxCloudless: stitch the public WMTS level into one global GeoTIFF on Earth's sphere."""

import math
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.windows import Window

from tile_service.cube import EARTH_RADIUS, equirect_affine, equirect_crs
from tile_service.runtime import atomic_json, check, log, read_json, run, sha256, stop

# Geographic matrix from the EOX WMTS WGS84 set: level 0 is 2 by 1 tiles of 256.
# https://tiles.maps.eox.at/wmts/1.0.0/WMTSCapabilities.xml
TEMPLATE = (
    "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020/default/WGS84/{matrix}/{row}/{col}.jpg"
)
CREDIT = (
    "EOxCloudless https://cloudless.eox.at by EOX IT Services GmbH "
    "(Contains modified Copernicus Sentinel data 2020). CC BY-NC-SA 4.0."
)
TILE = 256


def _grid(zoom):
    cols = 2 ** (zoom + 1)
    rows = 2**zoom
    width = cols * TILE
    height = rows * TILE
    return cols, rows, width, height, 2 * math.pi * EARTH_RADIUS / width


def describe(zoom, template=TEMPLATE):
    cols, rows, width, height, meters = _grid(zoom)
    return (
        f"{CREDIT}\n"
        f"Zoom {zoom}: {cols} x {rows} tiles, {width} x {height}, {round(meters)} m/pixel\n"
        f"{cols * rows} requests\n"
        f"{template}"
    )


def _tile_url(template, matrix, row, col):
    return template.format(matrix=matrix, row=row, col=col)


def _fetch(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    parsed = urlparse(url)
    try:
        if parsed.scheme == "file":
            shutil.copyfile(url2pathname(unquote(parsed.path)), part)
        else:
            run(
                [
                    "curl",
                    "--fail",
                    "--location",
                    "--silent",
                    "--show-error",
                    "--retry",
                    "5",
                    "--retry-all-errors",
                    "--retry-delay",
                    "2",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    "120",
                    "--user-agent",
                    "tile-service cloudless",
                    "--output",
                    str(part),
                    url,
                ]
            )
    except Exception as exc:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"Tile download failed: {url}") from exc
    os.replace(part, dest)


def _download(template, cache, zoom, cols, rows, jobs):
    pending = []
    for row in range(rows):
        for col in range(cols):
            dest = cache / str(row) / f"{col}.jpg"
            if dest.exists() and dest.stat().st_size > 0:
                continue
            pending.append((dest, _tile_url(template, zoom, row, col)))

    failed = []

    def grab(item):
        check()
        dest, url = item
        if dest.exists() and dest.stat().st_size > 0:
            return
        try:
            _fetch(url, dest)
        except Exception:
            if stop.is_set():
                raise
            failed.append(url)

    if not pending:
        return
    log(f"Downloading {len(pending)} tiles with {jobs} fetchers")
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        list(pool.map(grab, pending))
    if failed:
        raise RuntimeError(f"Tile download failed ({len(failed)}): {failed[0]}")


def _rgb(tile):
    data = tile.read(out_dtype="uint8")
    if data.shape == (3, TILE, TILE):
        return data
    if data.shape != (1, TILE, TILE):
        raise RuntimeError(f"Expected a 256x256 tile, got {data.shape}")
    # Some published cells are a one-color PNG rather than a JPEG.
    try:
        table = tile.colormap(1)
    except Exception:
        table = None
    if not table:
        return np.repeat(data, 3, axis=0)
    rgb = np.zeros((3, TILE, TILE), np.uint8)
    index = data[0]
    for key, color in table.items():
        rgb[:, index == key] = np.asarray(color[:3], np.uint8)[:, None]
    return rgb


def _write(output, cache, cols, rows, width, height):
    # Published longitude and geodetic latitude stay the sphere's angles, so a
    # later place at the same label lands on this pixel.
    transform = equirect_affine(EARTH_RADIUS, width, height)
    temp = Path(str(output) + ".tmp")
    try:
        with rasterio.open(
            temp,
            "w",
            driver="GTiff",
            width=width,
            height=height,
            count=3,
            dtype="uint8",
            crs=equirect_crs(EARTH_RADIUS),
            transform=transform,
            tiled=True,
            compress="lzw",
            bigtiff="IF_SAFER",
        ) as dest:
            dest.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
            for row in range(rows):
                check()
                log(f"Row {row + 1}/{rows}")
                for col in range(cols):
                    path = cache / str(row) / f"{col}.jpg"
                    try:
                        with rasterio.open(path) as tile:
                            data = _rgb(tile)
                    except Exception as exc:
                        raise RuntimeError(f"Unreadable tile: {path}") from exc
                    dest.write(data, window=Window(col * TILE, row * TILE, TILE, TILE))
        return temp
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def mosaic(output, *, zoom=7, template=TEMPLATE, jobs=16):
    """Download (resumably) and stitch one WMTS zoom level into `output`.

    An existing mosaic must match its receipt; it is reused, never replaced.
    """
    output = Path(output)
    # shortcut: zoom above 7 is rejected, about 600 m and 32768 requests. Use a paid GeoTIFF past that.
    if not 0 <= zoom <= 7:
        raise RuntimeError("Zoom must be 0..7")
    # shortcut: 16 fetchers. One reset used to abort the rest. Raise the cap if 16 stays slow and the service holds.
    if not 1 <= jobs <= 32:
        raise RuntimeError("Jobs must be 1..32")
    for token in ("{matrix}", "{row}", "{col}"):
        if token not in template:
            raise RuntimeError("URL template must use {matrix}, {row}, and {col}")
    cols, rows, width, height, _ = _grid(zoom)
    receipt_path = Path(str(output) + ".receipt.json")
    identity = {
        "urlTemplate": template,
        "zoom": zoom,
        "width": width,
        "height": height,
        "radiusMeters": EARTH_RADIUS,
    }
    if output.exists():
        if not receipt_path.exists():
            raise RuntimeError(
                f"Existing mosaic has no receipt; remove it or choose another path: {output}"
            )
        old = read_json(receipt_path)
        if any(old.get(key) != value for key, value in identity.items()) or old.get(
            "sha256"
        ) != sha256(output):
            raise RuntimeError(f"Existing mosaic belongs to another download: {output}")
        log(f"Verified existing mosaic; no download needed: {output}")
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(output.parent).free
    if free < width * height * 3 + 64 * 1024 * 1024:
        raise RuntimeError("Insufficient disk space for the mosaic.")
    log(describe(zoom, template))
    cache = output.parent / f"{output.stem}-tiles" / str(zoom)
    _download(template, cache, zoom, cols, rows, jobs)
    check()
    temp = _write(output, cache, cols, rows, width, height)
    identity["sha256"] = sha256(temp)
    atomic_json(receipt_path, identity)
    os.replace(temp, output)
    log(f"Mosaic: {output}")
    return output
