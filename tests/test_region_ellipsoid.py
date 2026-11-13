import math

import numpy as np
import rasterio
from rasterio.transform import from_origin

from tile_service import heights
from tile_service.runtime import radii

EARTH = 6378137.0


def _dem(path, crs="EPSG:4326"):
    # 0.001° pixels around 26°N, 97°W, every sample 12 m.
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=200,
        height=200,
        count=1,
        dtype="float32",
        crs=crs,
        transform=from_origin(-97.1, 26.1, 0.001, 0.001),
        nodata=-9999,
    ) as dst:
        dst.write(np.full((200, 200), 12, np.float32), 1)
    return path


def test_a_dem_on_the_bodys_ellipsoid_is_warped_onto_its_sphere(tmp_path):
    source = _dem(tmp_path / "dem.tif")
    warped = heights._onto_sphere(source, EARTH, 26.0, -97.0, 2000)
    assert warped != source
    with rasterio.open(warped) as src:
        major, minor = radii(src.crs)
        assert abs(major - EARTH) < 0.01 and abs(minor - EARTH) < 0.01
        # Geodetic degrees carry over as sphere degrees: the site center stays at 26°N, 97°W.
        x, y = math.radians(-97.0) * EARTH, math.radians(26.0) * EARTH
        row, col = src.index(x, y)
        assert src.read(1)[row, col] == 12
        # The copy covers the site disk and not the whole source.
        assert src.width * src.res[0] < 6000


def test_other_sources_pass_through(tmp_path):
    source = _dem(tmp_path / "dem.tif")
    # A different body, and a source already on the sphere, are left for the usual checks.
    assert heights._onto_sphere(source, 1737400.0, 26.0, -97.0, 2000) == source
    sphere = _dem(tmp_path / "sphere.tif", crs=f"+proj=longlat +R={EARTH} +no_defs")
    assert heights._onto_sphere(sphere, EARTH, 26.0, -97.0, 2000) == sphere
