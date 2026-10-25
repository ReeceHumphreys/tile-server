import json
import subprocess
import sys
from pathlib import Path

import pytest
import rasterio

ROOT = Path(__file__).resolve().parents[1]


def load(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value))


def run(*args, ok=True):
    result = subprocess.run(
        [sys.executable, "-m", "pipeline", *map(str, args)],
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


def fixture(path, *args):
    run("fixture", "--output", path, *args)
    return path


def build(source, output, *args):
    run("tiles", "--source", source, "--output", output, "--tile-size", 32, "--max-level", 1, *args)
    return load(output / "manifest.json")


def pixels(path):
    with rasterio.open(path) as source:
        return source.read().transpose(1, 2, 0)


@pytest.fixture
def source(tmp_path):
    return fixture(tmp_path / "source.tif")
