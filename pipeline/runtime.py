import hashlib
import json
import math
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

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import ColorInterp
from rasterio.errors import NotGeoreferencedWarning
from rasterio.transform import Affine

# PNG tiles carry no geotransform; the cube frame is in the manifest.
warnings.filterwarnings("ignore", category=NotGeoreferencedWarning)

PI = math.pi
MOON_RADIUS = 1_737_400
EARTH_RADIUS = 6_378_137
DEM_URL = "https://svs.gsfc.nasa.gov/vis/a000000/a004700/a004720/ldem_16_uint.tif"
DEM_SHA = "45a2b32d56e81ed30db07fead8abc842b249b6511219d9ca2c53f81bc2dc5d62"
FACE_LON = (0, 180, 0, 0, -90, 90)
FACE_LAT = (0, 0, 90, -90, 0, 0)
RGBA = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)

stop = threading.Event()
_log_lock = threading.Lock()


def install_signals():
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


class Args:
    def __init__(self, argv):
        if len(argv) < 2:
            raise RuntimeError("Usage: tile-service COMMAND [options]; use tile-service --help")
        self.command = argv[1]
        self.values = {}
        flags = {"--info", "--keep-cache", "--earth", "--nodata", "--self-test", "--all"}
        i = 2
        while i < len(argv):
            key = argv[i]
            if not key.startswith("--"):
                raise RuntimeError(f"Expected option: {key}")
            if key in flags or (key == "--height" and self.command == "fixture"):
                self.values[key] = "true"
                i += 1
            else:
                if i + 1 >= len(argv):
                    raise RuntimeError(f"Missing value for {key}")
                self.values[key] = argv[i + 1]
                i += 2

    def get(self, key, fallback=""):
        return self.values.get(key, fallback)

    def has(self, key):
        return key in self.values

    def integer(self, key, fallback):
        if not self.has(key):
            return fallback
        text = self.values[key]
        if re.fullmatch(r"[+-]?\d+", text) is None:
            raise RuntimeError(f"Expected integer for {key}")
        return int(text)

    def validate(self, allowed):
        allowed = set(allowed)
        for key in self.values:
            if key not in allowed:
                raise RuntimeError(f"Unknown option: {key}")


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def config(args):
    return read_json(args.get("--config", catalog_path("lroc-wac-100m.json")))


def catalog_path(name):
    """Catalogs work in both an editable checkout and an installed wheel."""
    packaged = Path(__file__).parent / "datasets" / name
    return packaged if packaged.exists() else Path(__file__).parent.parent / "datasets" / name


def dump(value):
    return json.dumps(value, indent=2) + "\n"


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
    atomic_write(path, dump(value))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


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


def validate_source(src, radius):
    if src.count not in (1, 3):
        raise RuntimeError("Expected 1-band or RGB lunar source")
    if any(dtype != "uint8" for dtype in src.dtypes):
        raise RuntimeError("Expected uint8 source")
    require_sphere(
        src.crs,
        radius,
        "Source CRS must use the configured lunar sphere, not an Earth CRS",
    )


def sphere_crs(proj):
    return CRS.from_proj4(proj + " +units=m +no_defs")


def face_crs(face, radius):
    return sphere_crs(f"+proj=gnom +lat_0={FACE_LAT[face]} +lon_0={FACE_LON[face]} +R={radius}")


def tile_affine(face, level, x, y, size, gutter, radius):
    scale = 2**level
    pixel = 2 * radius / (scale * size)
    left = 2 * radius * (x / scale - 0.5) - gutter * pixel
    top = 2 * radius * ((y + 1) / scale - 0.5) + gutter * pixel
    if face == 2:
        geo = (top, 0, -pixel, -left, -pixel, 0)
    elif face == 3:
        geo = (-top, 0, pixel, left, pixel, 0)
    else:
        geo = (left, pixel, 0, top, 0, -pixel)
    return Affine(geo[1], geo[2], geo[0], geo[4], geo[5], geo[3])


def direction(face, u, v):
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    one = np.ones_like(u)
    if face == 0:
        vector = np.stack((one, v, -u))
    elif face == 1:
        vector = np.stack((-one, v, u))
    elif face == 2:
        vector = np.stack((u, one, -v))
    elif face == 3:
        vector = np.stack((u, -one, v))
    elif face == 4:
        vector = np.stack((u, v, one))
    else:
        vector = np.stack((-u, v, -one))
    return vector / np.linalg.norm(vector, axis=0)


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
