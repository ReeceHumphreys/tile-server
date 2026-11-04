"""Exercise the installed tools image without pytest or the source checkout."""

import json
import tempfile
from pathlib import Path

import numpy as np
import rasterio

from tile_service import imagery, releases, sources
from tile_service.cube import MOON_RADIUS, equirect_affine, equirect_crs
from tile_service.runtime import catalog_path, gdal_library, read_json


def raster(path, value):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=64,
        height=32,
        count=3,
        dtype="uint8",
        crs=equirect_crs(MOON_RADIUS),
        transform=equirect_affine(MOON_RADIUS, 64, 32),
    ) as dataset:
        dataset.write(np.full((3, 32, 64), value, np.uint8))
    return path


def main():
    assert gdal_library().is_file()
    dataset = json.loads(catalog_path("lroc-wac-100m.json").read_text())
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = raster(root / "source.tif", 120)
        imagery.build(source, root / "build/moon/imagery", dataset, tile_size=16, max_level=1)
        releases.pack(root / "build")
        releases.export(root / "build", root / "release")
        assert read_json(root / "release/release.json")["assetCount"] == 7
        parts = [raster(root / f"part{index}.tif", 40 * index) for index in range(2)]
        assert sources.mosaic(parts, root / "mosaic.vrt", MOON_RADIUS).exists()
    print("Installed tools: imagery, PMTiles, release export, and GDAL VRT checks passed")


if __name__ == "__main__":
    main()
