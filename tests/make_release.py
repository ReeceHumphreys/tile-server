"""Small real release for container smoke tests; no network datasets required."""

import sys
import tempfile
from pathlib import Path

from pipeline.archives import pack_document
from pipeline.publish import publish
from pipeline.runtime import Args
from tests.conftest import build, fixture


def main(output):
    with tempfile.TemporaryDirectory(prefix="tile-smoke-") as directory:
        root = Path(directory)
        dataset = root / "build/moon/imagery"
        build(fixture(root / "source.tif"), dataset)
        pack_document(dataset / "manifest.json")
        publish(
            Args(["tile-service", "publish", "--root", str(root / "build"), "--output", output])
        )


if __name__ == "__main__":
    main(sys.argv[1])
