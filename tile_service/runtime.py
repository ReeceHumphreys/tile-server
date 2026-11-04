"""Process plumbing shared by every build: interrupts, logging, files and hashes."""

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import warnings
from pathlib import Path

# Set before rasterio loads GDAL. One warp thread matches the old gdalwarp flags.
os.environ.setdefault("GDAL_CACHEMAX", "256")
os.environ.setdefault("GDAL_NUM_THREADS", "1")
os.environ.setdefault("GDAL_PAM_ENABLED", "NO")

import rasterio
from rasterio.enums import ColorInterp
from rasterio.errors import NotGeoreferencedWarning

# PNG tiles carry no geotransform; the cube frame is in the manifest.
warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)

RGBA = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)

stop = threading.Event()
_log_lock = threading.Lock()


def install_signals():
    """First SIGINT/SIGTERM asks builds to stop at a safe point; a second one kills."""

    def handle(signum, frame):
        if stop.is_set():
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)


def check():
    if stop.is_set():
        raise RuntimeError("Build interrupted; rerun to resume.")


def log(message):
    with _log_lock:
        print(message, flush=True)


def gdal_version():
    return rasterio.__gdal_version__


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def catalog_path(name):
    """Catalogs work in both an editable checkout and an installed wheel."""
    packaged = Path(__file__).parent / "datasets" / name
    return packaged if packaged.exists() else Path(__file__).parent.parent / "datasets" / name


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        temp.write_text(data, encoding="utf-8")
    else:
        temp.write_bytes(data)
    os.replace(temp, path)


def atomic_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + "\n")


_digests = {}


def sha256(path):
    """Content hash, memoized per (path, size, mtime) so a build hashes a source once."""
    path = Path(path)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key in _digests:
        return _digests[key]
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    _digests[key] = digest.hexdigest()
    return _digests[key]


def run(args):
    result = subprocess.run(args)
    if result.returncode != 0:
        raise RuntimeError(f"{args[0]} failed; partial files retained for resume.")


def radii(crs):
    match = re.search(
        r'SPHEROID\["[^"]*",\s*([0-9.eE+-]+)\s*,\s*([0-9.eE+-]+)',
        crs.to_wkt(),
    )
    if not match:
        raise RuntimeError("Source must be georeferenced")
    major = float(match.group(1))
    inverse = float(match.group(2))
    minor = major if inverse == 0 else major * (1 - 1 / inverse)
    return major, minor


def require_sphere(crs, radius, message):
    if crs is None:
        raise RuntimeError(message)
    major, minor = radii(crs)
    if abs(major - radius) >= 0.01 or abs(minor - radius) >= 0.01:
        raise RuntimeError(message)


def validate_imagery(src, radius):
    if src.count not in (1, 3):
        raise RuntimeError("Expected 1-band or RGB imagery source")
    if any(dtype != "uint8" for dtype in src.dtypes):
        raise RuntimeError("Expected uint8 source")
    require_sphere(
        src.crs,
        radius,
        "Source CRS must use the configured body sphere (a lunar source needs the lunar sphere)",
    )


def write_png(path, rgba, compression):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with rasterio.open(
        temp,
        "w",
        driver="PNG",
        width=rgba.shape[2],
        height=rgba.shape[1],
        count=4,
        dtype="uint8",
        zlevel=compression,
    ) as dataset:
        dataset.colorinterp = RGBA
        dataset.write(rgba)
    check()
    os.replace(temp, path)


def gdal_library():
    root = Path(rasterio.__file__).parent
    for folder in (root / ".dylibs", root / ".libs", root.parent / "rasterio.libs"):
        hits = sorted(folder.glob("libgdal*"))
        if hits:
            return hits[0]
    raise RuntimeError("rasterio GDAL library not found")
