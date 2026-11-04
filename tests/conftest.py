import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from tile_service import imagery
from tile_service.cube import MOON_RADIUS, equirect_affine, equirect_crs, sphere_crs
from tile_service.runtime import catalog_path

ROOT = Path(__file__).resolve().parents[1]
WAC = json.loads(catalog_path("lroc-wac-100m.json").read_text())


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value))


def run(*args, ok=True):
    """Run the installed CLI in a subprocess; returns combined output."""
    result = subprocess.run(
        [sys.executable, "-m", "tile_service", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout
    return result.stdout + result.stderr


def fixture(
    path,
    *,
    constant=None,
    nodata=False,
    height=False,
    radius=MOON_RADIUS,
    polar_quadrant=None,
):
    """A 720x360 analytic raster: RGB encodes the sphere direction unless `constant`.

    `height` writes one float band. `nodata` blanks the north-east quadrant.
    `polar_quadrant` (0..3) places it in a 30 km south-polar stereographic square.
    """
    bands = 1 if height else 3
    y, x = np.mgrid[:360, :720]
    lon = np.radians(x * 0.5 - 179.75)
    lat = np.radians(89.75 - y * 0.5)
    direction = (np.cos(lat) * np.sin(lon), np.sin(lat), np.cos(lat) * np.cos(lon))
    pixels = np.empty((bands, 360, 720), np.float32 if height else np.uint8)
    for band in range(bands):
        pixels[band] = (
            constant if constant is not None else np.rint(127 + 100 * direction[band]).astype(int)
        )
        if nodata:
            pixels[band, :180, 360:] = 0
    if polar_quadrant is not None:
        left = 0 if polar_quadrant < 2 else -30000
        top = 30000 if polar_quadrant in (0, 3) else 0
        crs = sphere_crs(f"+proj=stere +lat_0=-90 +lat_ts=-90 +lon_0=0 +R={radius}")
        transform = Affine(30000 / 720, 0, left, 0, -30000 / 360, top)
    else:
        crs = equirect_crs(radius)
        transform = equirect_affine(radius, 720, 360)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=720,
        height=360,
        count=bands,
        dtype=pixels.dtype,
        crs=crs,
        transform=transform,
        nodata=0 if nodata else None,
    ) as dataset:
        dataset.write(pixels)
    return Path(path)


def build(source, output, dataset=WAC, **options):
    """A small imagery pyramid: 32 px tiles to level 1 unless overridden."""
    settings = {"tile_size": 32, "max_level": 1, **options}
    return imagery.build(source, output, dataset, **settings)


def pixels(path):
    with rasterio.open(path) as source:
        return source.read().transpose(1, 2, 0)


@pytest.fixture
def source(tmp_path):
    return fixture(tmp_path / "source.tif")
