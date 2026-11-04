"""Cube-sphere geometry and the shared uint16 height encoding.

Faces are gnomonic projections of a sphere. Tile addresses are face-local,
bottom-left origin, Y up. Every module that maps between faces, tiles, sphere
directions or encoded heights goes through here.
"""

import math
from typing import NamedTuple

import numpy as np
from rasterio.crs import CRS
from rasterio.transform import Affine

MOON_RADIUS = 1_737_400
EARTH_RADIUS = 6_378_137
FACE_LON = (0, 180, 0, 0, -90, 90)
FACE_LAT = (0, 0, 90, -90, 0, 0)
MAX_REGION_TILES = 20000

# uint16 heights: meters = value * 0.5 - 10000.
HEIGHT_SCALE = 0.5
HEIGHT_OFFSET = -10000
HEIGHT_MIN = -10000
HEIGHT_MAX = 22767.5


class Tile(NamedTuple):
    face: int
    level: int
    x: int
    y: int

    @property
    def key(self):
        return f"{self.face}/{self.level}/{self.x}/{self.y}"

    @classmethod
    def parse(cls, key):
        return cls(*(int(part) for part in key.split("/")))

    def ancestor(self, level):
        shift = self.level - level
        return Tile(self.face, level, self.x >> shift, self.y >> shift)


def pyramid(level):
    """Every tile of one face, coarse to fine, as (level, x, y)."""
    for depth in range(level + 1):
        for y in range(1 << depth):
            for x in range(1 << depth):
                yield depth, x, y


# Heights ---------------------------------------------------------------------


def encode_heights(meters):
    meters = np.asarray(meters, dtype=float)
    if meters.size and (
        not np.isfinite(meters).all()
        or float(meters.min()) < HEIGHT_MIN
        or float(meters.max()) > HEIGHT_MAX
    ):
        raise RuntimeError(
            f"Height outside the uint16 encoding range [{HEIGHT_MIN}, {HEIGHT_MAX}] m; "
            "check source units/datum/nodata"
        )
    return np.rint((meters - HEIGHT_OFFSET) / HEIGHT_SCALE).astype(np.uint16)


def decode_heights(encoded):
    return np.asarray(encoded).astype(np.float64) * HEIGHT_SCALE + HEIGHT_OFFSET


def height_range(encoded):
    decoded = decode_heights(encoded)
    return float(decoded.min()), float(decoded.max())


# Projections -----------------------------------------------------------------


def sphere_crs(proj):
    return CRS.from_proj4(proj + " +units=m +no_defs")


def face_crs(face, radius):
    return sphere_crs(f"+proj=gnom +lat_0={FACE_LAT[face]} +lon_0={FACE_LON[face]} +R={radius}")


def equirect_crs(radius):
    return sphere_crs(f"+proj=eqc +R={radius}")


def equirect_affine(radius, width, height):
    """Whole-sphere equirectangular grid, north-up, -180..180 east."""
    return Affine(
        2 * math.pi * radius / width,
        0,
        -math.pi * radius,
        0,
        -math.pi * radius / height,
        math.pi * radius / 2,
    )


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


# Directions ------------------------------------------------------------------


def lonlat_vector(latitude_deg, longitude_deg):
    lat = latitude_deg * math.pi / 180
    lon = longitude_deg * math.pi / 180
    return np.array([math.cos(lat) * math.cos(lon), math.sin(lat), -math.cos(lat) * math.sin(lon)])


def direction(face, u, v):
    """Unit sphere directions for face coordinates u, v in [-1, 1]."""
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


def tile_directions(tile, size, gutter):
    """(rows, cols, directions) for every pixel of a guttered tile."""
    width = size + 2 * gutter
    rows, cols = np.indices((width, width))
    count = 2**tile.level
    uu = 2 * (tile.x + (cols - gutter + 0.5) / size) / count - 1
    vv = 2 * (tile.y + 1 - (rows - gutter + 0.5) / size) / count - 1
    return rows, cols, direction(tile.face, uu, vv)


def _face_parts(vectors):
    """Face id and face-local (u, v, w) for directions shaped (3, ...)."""
    x, y, z = vectors
    axis = np.argmax(np.abs(vectors), axis=0)
    positive = np.take_along_axis(vectors, axis[None, ...], axis=0)[0] >= 0
    face = np.empty(x.shape, np.int64)
    u = np.empty(x.shape, np.float64)
    v = np.empty(x.shape, np.float64)
    w = np.empty(x.shape, np.float64)
    for mask, face_id, uu, vv, ww in (
        ((axis == 0) & positive, 0, -z, y, x),
        ((axis == 0) & ~positive, 1, z, y, -x),
        ((axis == 1) & positive, 2, x, -z, y),
        ((axis == 1) & ~positive, 3, x, z, -y),
        ((axis == 2) & positive, 4, x, y, z),
        ((axis == 2) & ~positive, 5, -x, y, -z),
    ):
        face[mask] = face_id
        u[mask] = uu[mask]
        v[mask] = vv[mask]
        w[mask] = ww[mask]
    return face, u, v, w


def face_uv(vectors):
    """Face id and 0..1 face UV for directions shaped (3, ...) or (3,)."""
    vectors = np.asarray(vectors, dtype=float)
    face, u, v, w = _face_parts(vectors)
    return face, (1 + u / w) / 2, (1 + v / w) / 2


# Disks -----------------------------------------------------------------------


def _disk_bounds(center, extent, body_radius, message):
    """Face and UV bounds of a spherical cap; the cap must stay on one face."""
    face, u, v, w = (float(part) for part in _face_parts(np.asarray(center, dtype=float)))
    delta = 2 * math.sin(extent / body_radius / 2)

    def interval(q):
        points = (
            (q - delta) / (w - delta),
            (q + delta) / (w - delta),
            (q - delta) / (w + delta),
            (q + delta) / (w + delta),
        )
        return (min(points) + 1) / 2, (max(points) + 1) / 2

    us, vs = interval(u), interval(v)
    if not (us[0] > 0 and us[1] < 1 and vs[0] > 0 and vs[1] < 1):
        raise RuntimeError(message)
    return int(face), us, vs


def _disk_tiles(face, us, vs, levels):
    tiles = []
    for level in levels:
        count = 1 << level
        xs = range(math.floor(us[0] * count), math.floor(us[1] * count) + 1)
        ys = range(math.floor(vs[0] * count), math.floor(vs[1] * count) + 1)
        if len(tiles) + len(xs) * len(ys) > MAX_REGION_TILES:
            raise RuntimeError(
                f"Region exceeds {MAX_REGION_TILES:,} tiles per layer; lower level or radius"
            )
        tiles.extend(Tile(face, level, x, y) for y in ys for x in xs)
    return tiles


class Coverage:
    """A detail disk, optionally inside a larger single-face overview disk.

    `tiles` lists every address to generate, coarse to fine; `weight(distance)`
    is the blend of new data over the base at a surface distance from `center`.
    """

    def __init__(
        self,
        latitude,
        longitude,
        extent,
        body_radius,
        base_level,
        max_level,
        overview=None,
        feather=None,
    ):
        if not base_level < max_level <= 16:
            raise RuntimeError("Regional max level must exceed global depth and be at most 16")
        if not (
            math.isfinite(latitude)
            and math.isfinite(longitude)
            and math.isfinite(extent)
            and abs(latitude) <= 90
            and abs(longitude) <= 180
            and 100 <= extent <= 20000
        ):
            raise RuntimeError("Invalid regional center/radius")
        inner = lonlat_vector(latitude, longitude)
        face, us, vs = _disk_bounds(
            inner, extent, body_radius, "Region crosses a cube face boundary; reduce its radius"
        )
        tiles = _disk_tiles(face, us, vs, range(base_level + 1, max_level + 1))
        self.face = face
        self.center = inner
        self.radius = extent
        self.feather = min(100.0, extent * 0.1)
        self.level = 0
        self.description = None
        if overview:
            lat = float(overview["latitudeDeg"])
            lon = float(overview["longitudeDeg"])
            self.radius = float(overview["radiusMeters"])
            self.level = int(overview["maxLevel"])
            if not (
                math.isfinite(lat)
                and abs(lat) <= 90
                and math.isfinite(lon)
                and abs(lon) <= 180
                and math.isfinite(self.radius)
                and extent <= self.radius <= 100000
                and base_level < self.level <= max_level
            ):
                raise RuntimeError("Invalid overview coverage")
            self.center = lonlat_vector(lat, lon)
            if body_radius * np.linalg.norm(self.center - inner) + extent > self.radius:
                raise RuntimeError("Overview must contain the detail region")
            outer_face, ous, ovs = _disk_bounds(
                self.center, self.radius, body_radius, "Overview crosses a cube-face boundary"
            )
            if outer_face != face:
                raise RuntimeError("Overview and detail must share one cube face")
            tiles += _disk_tiles(face, ous, ovs, range(base_level + 1, self.level + 1))
            tiles = sorted(set(tiles), key=lambda t: (t.level, t.face, t.y, t.x))
            if len(tiles) > MAX_REGION_TILES:
                raise RuntimeError(f"Region exceeds {MAX_REGION_TILES:,} tiles per layer")
            self.description = overview
        if feather is not None:
            self.feather = float(feather)
        if not (
            math.isfinite(self.feather) and self.feather > 0 and self.feather <= self.radius * 0.5
        ):
            raise RuntimeError("Invalid coverage feather")
        self.tiles = tiles

    def distance(self, directions, body_radius):
        return body_radius * np.linalg.norm(directions - self.center.reshape(3, 1, 1), axis=0)

    def weight(self, distance):
        blend = np.clip((self.radius - np.asarray(distance, dtype=float)) / self.feather, 0, 1)
        if self.level:
            return blend * blend * (3 - 2 * blend)
        return blend
