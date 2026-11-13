"""Draw GeoJSON polygons as a whole-world raster, for maps with no photograph behind them."""

import json
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import Affine

from tile_service.roots import equirect_affine, equirect_crs
from tile_service.runtime import check, log

ROWS_PER_STRIP = 512
# Subpixels along each axis, so coasts are antialiased rather than stairs.
SAMPLES = 3


def draw(geojson, out, *, radius, width, fill, background):
    """Write `geojson`'s polygons as `fill` over `background`, one grey band, `width` wide."""
    out = Path(out)
    height = width // 2
    shapes = [feature["geometry"] for feature in json.loads(Path(geojson).read_text())["features"]]
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    log(f"Drawing {len(shapes)} polygons at {width} x {height}")
    with rasterio.open(
        temp,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs=equirect_crs(radius),
        transform=equirect_affine(radius, width, height),
        tiled=True,
        compress="deflate",
        bigtiff="IF_SAFER",
    ) as dest:
        degrees = 360 / (width * SAMPLES)
        for top in range(0, height, ROWS_PER_STRIP):
            check()
            rows = min(ROWS_PER_STRIP, height - top)
            covered = rasterize(
                shapes,
                out_shape=(rows * SAMPLES, width * SAMPLES),
                transform=Affine(degrees, 0, -180, 0, -degrees, 90 - top * SAMPLES * degrees),
                dtype="uint8",
            )
            share = covered.reshape(rows, SAMPLES, width, SAMPLES).mean(axis=(1, 3))
            pixels = np.rint(background + (fill - background) * share).astype("uint8")
            dest.write(pixels, 1, window=((top, top + rows), (0, width)))
    os.replace(temp, out)
    return out
