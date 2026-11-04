"""Small real release for container smoke tests; no network datasets required."""

import sys
import tempfile
from pathlib import Path

from tests.conftest import build, fixture
from tile_service.releases import export, pack


def main(output):
    with tempfile.TemporaryDirectory(prefix="tile-smoke-") as directory:
        root = Path(directory)
        build(fixture(root / "source.tif"), root / "build/moon/imagery")
        pack(root / "build")
        export(root / "build", output)


if __name__ == "__main__":
    main(sys.argv[1])
