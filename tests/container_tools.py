"""Exercise the installed tools image without pytest or the source checkout."""

import tempfile
from pathlib import Path

from pipeline.jobs import execute
from pipeline.runtime import atomic_json, gdal_library, read_json


def main():
    assert gdal_library().is_file()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.tif"
        output = root / "build/moon/imagery"
        execute("fixture", "--output", source)
        execute(
            "tiles", "--source", source, "--output", output, "--tile-size", 16, "--max-level", 1
        )
        execute("pack", "--manifest", output / "manifest.json")
        execute("publish", "--root", root / "build", "--output", root / "release")
        assert read_json(root / "release/release.json")["assetCount"] == 7
        paths = []
        for quadrant in range(4):
            path = root / f"q{quadrant}.tif"
            execute("fixture", "--output", path, "--polar-quadrant", quadrant)
            paths.append(str(path))
        atomic_json(root / "sources.json", paths)
        execute("mosaic", "--sources", root / "sources.json", "--output", root / "polar.vrt")
        assert (root / "polar.vrt").exists()
    print("Installed tools: imagery, PMTiles, release export, and GDAL VRT checks passed")


if __name__ == "__main__":
    main()
