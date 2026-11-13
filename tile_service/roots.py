"""The tile grid and the uint16 height encoding.

Four roots, each its own quadtree with u and v from 0 to 1 and y up. Roots 0 and 1 are the
western and eastern hemispheres in latitude and longitude; 2 and 3 are north and south
polar caps, in stereographic projection of the surface normal. A cap tile matches the
ground size of a geographic tile `CAP_LEVEL_OFFSET` levels deeper, so a dataset's level means
one resolution everywhere. `spec/README.md` defines the grid and `spec/conformance/grid.json`
checks this module. Every module that maps between roots, tiles, directions or encoded
heights goes through here.
"""

import math
from typing import NamedTuple

import numpy as np
from rasterio.crs import CRS
from rasterio.transform import Affine

MOON_RADIUS = 1_737_400
EARTH_RADIUS = 6_378_137
ROOTS = range(4)
WEST, EAST, NORTH, SOUTH = ROOTS
CAP_HALF = math.pi / 32
CAP_LEVEL_OFFSET = 3
SIN_JOIN_MIDDLE = math.sin(math.radians(80.5))
MAX_REGION_TILES = 20000

# Default uint16 heights: meters = value * 0.5 - 10000. Manifests may set their own.
HEIGHT_SCALE = 0.5
HEIGHT_OFFSET = -10000


def is_cap(root):
    return root >= NORTH


def root_max_level(root, level):
    """A root's deepest level for a dataset level, the only conversion between the two."""
    return max(0, level - CAP_LEVEL_OFFSET) if is_cap(root) else level


def root_width(root, radius):
    """Meters across a root, at the equator or the pole."""
    return math.pi * radius / (1 << CAP_LEVEL_OFFSET if is_cap(root) else 1)


class Tile(NamedTuple):
    root: int
    level: int
    x: int
    y: int

    @property
    def key(self):
        return f"{self.root}/{self.level}/{self.x}/{self.y}"

    @classmethod
    def parse(cls, key):
        return cls(*(int(part) for part in key.split("/")))

    def ancestor(self, level):
        shift = self.level - level
        return Tile(self.root, level, self.x >> shift, self.y >> shift)


def pyramid(level):
    """Every tile of one root, coarse to fine, as (level, x, y)."""
    for depth in range(level + 1):
        for y in range(1 << depth):
            for x in range(1 << depth):
                yield depth, x, y


def tile_count(level):
    """Tiles in every root of a dataset reaching `level`."""
    return sum(((1 << (2 * (root_max_level(root, level) + 1))) - 1) // 3 for root in ROOTS)


# Sources ---------------------------------------------------------------------


def east_west_pixels(latitude_degrees, width):
    """Odd count of lat-long pixels spanning one pixel's height on the ground, at most `width`."""
    cosine = max(math.cos(math.radians(latitude_degrees)), 1 / width)
    return min(int(round(1 / cosine)) | 1, width | 1)


def smooth_row(row, count, mode):
    """Box-average a row of pixels along longitude. Lat-long pixels narrow toward the pole,
    so left alone they keep noise in stripes far thinner than the map's real resolution."""
    half = count // 2
    padded = np.pad(row.astype(np.uint32), (half + 1, half), mode=mode)
    sums = np.cumsum(padded)
    return ((sums[count:] - sums[:-count]) // count).astype(row.dtype)


def global_equirect(src, radius):
    """Whether `src` is one whole-world equirectangular raster."""
    text = src.crs.to_proj4()
    span = src.width * abs(src.transform.a)
    whole = math.tau * radius if "+proj=eqc" in text else 360.0
    return ("+proj=eqc" in text or src.crs.is_geographic) and abs(span - whole) < 2 * abs(
        src.transform.a
    )


def require_warpable(src, root, radius):
    """GDAL smears a polar target where it crosses a whole-world source's date line."""
    # shortcut: regional builds refuse this case, where global imagery samples around it.
    # Upgrade if a site on a cap ever needs a whole-world source.
    if is_cap(root) and global_equirect(src, radius):
        raise RuntimeError("A site on a polar cap cannot warp a whole-world source")


def bilinear(grid, xs, ys, wrap=False):
    """A 2-D grid at fractional pixel positions, clamped at its edges or wrapped east to west."""
    height, width = grid.shape
    xs = np.asarray(xs, dtype=float)
    ys = np.clip(np.asarray(ys, dtype=float), 0, height - 1)
    if not wrap:
        xs = np.clip(xs, 0, width - 1)
    ix = np.floor(xs).astype(np.int64)
    iy = np.floor(ys).astype(np.int64)
    fx = xs - ix
    fy = ys - iy

    def at(column, row):
        column = np.mod(column, width) if wrap else np.minimum(column, width - 1)
        return grid[np.minimum(row, height - 1), column]

    return (1 - fy) * ((1 - fx) * at(ix, iy) + fx * at(ix + 1, iy)) + fy * (
        (1 - fx) * at(ix, iy + 1) + fx * at(ix + 1, iy + 1)
    )


# Heights ---------------------------------------------------------------------


def encode_heights(meters, scale=HEIGHT_SCALE, offset=HEIGHT_OFFSET):
    meters = np.asarray(meters, dtype=float)
    top = 65535 * scale + offset
    if meters.size and (
        not np.isfinite(meters).all() or float(meters.min()) < offset or float(meters.max()) > top
    ):
        raise RuntimeError(
            f"Height outside the uint16 encoding range [{offset}, {top}] m; "
            "check source units/datum/nodata"
        )
    return np.rint((meters - offset) / scale).astype(np.uint16)


def decode_heights(encoded, scale=HEIGHT_SCALE, offset=HEIGHT_OFFSET):
    return np.asarray(encoded).astype(np.float64) * scale + offset


def height_range(encoded, scale=HEIGHT_SCALE, offset=HEIGHT_OFFSET):
    decoded = decode_heights(encoded, scale, offset)
    return float(decoded.min()), float(decoded.max())


# Projections -----------------------------------------------------------------


def sphere_crs(proj):
    return CRS.from_proj4(proj + " +units=m +no_defs")


def root_crs(root, radius):
    if root == NORTH:
        return sphere_crs(f"+proj=stere +lat_0=90 +lon_0=0 +k=1 +R={radius}")
    if root == SOUTH:
        return sphere_crs(f"+proj=stere +lat_0=-90 +lon_0=0 +k=1 +R={radius}")
    return equirect_crs(radius)


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


def tile_affine(root, level, x, y, size, gutter, radius):
    width = root_width(root, radius)
    scale = 2**level
    pixel = width / (scale * size)
    if is_cap(root):
        west = south = -width / 2
    else:
        west = -math.pi * radius if root == WEST else 0.0
        south = -width / 2
    left = west + width * x / scale - gutter * pixel
    top = south + width * (y + 1) / scale + gutter * pixel
    return Affine(pixel, 0, left, 0, -pixel, top)


# Directions ------------------------------------------------------------------


def lonlat_vector(latitude_deg, longitude_deg):
    lat = latitude_deg * math.pi / 180
    lon = longitude_deg * math.pi / 180
    return np.array([math.cos(lat) * math.cos(lon), math.sin(lat), -math.cos(lat) * math.sin(lon)])


def direction(root, u, v):
    """Unit surface normals at a root's u and v, which may run past 0..1."""
    u = np.asarray(u, dtype=float)
    v = np.asarray(v, dtype=float)
    if is_cap(root):
        x = (2 * u - 1) * CAP_HALF
        y = (2 * v - 1) * CAP_HALF
        s = 1 + x * x + y * y
        if root == NORTH:
            return np.stack((-2 * y / s, 2 / s - 1, -2 * x / s))
        return np.stack((2 * y / s, 1 - 2 / s, -2 * x / s))
    longitude = (u - (1 if root == WEST else 0)) * math.pi
    latitude = (v - 0.5) * math.pi
    return np.stack(
        (
            np.cos(latitude) * np.cos(longitude),
            np.sin(latitude),
            -np.cos(latitude) * np.sin(longitude),
        )
    )


def tile_directions(tile, size, gutter):
    """(rows, cols, directions) for every pixel of a guttered tile."""
    width = size + 2 * gutter
    rows, cols = np.indices((width, width))
    count = 2**tile.level
    uu = (tile.x + (cols - gutter + 0.5) / size) / count
    vv = (tile.y + 1 - (rows - gutter + 0.5) / size) / count
    return rows, cols, direction(tile.root, uu, vv)


def uv_on(root, vectors):
    """u and v of directions on one root, running past 0..1 beyond its edges."""
    x, y, z = np.asarray(vectors, dtype=float)
    length = np.sqrt(x * x + y * y + z * z)
    x, y, z = x / length, y / length, z / length
    if root == NORTH:
        return (-z / (1 + y) / CAP_HALF + 1) / 2, (-x / (1 + y) / CAP_HALF + 1) / 2
    if root == SOUTH:
        return (-z / (1 - y) / CAP_HALF + 1) / 2, (x / (1 - y) / CAP_HALF + 1) / 2
    longitude = np.arctan2(-z, x)
    return (
        longitude / math.pi + (1 if root == WEST else 0),
        np.arctan2(y, np.hypot(x, z)) / math.pi + 0.5,
    )


def root_uv(vectors):
    """The owning root, and its 0..1 u and v, for directions shaped (3, ...) or (3,)."""
    vectors = np.asarray(vectors, dtype=float)
    x, y, z = vectors
    sine = y / np.sqrt(x * x + y * y + z * z)
    root = np.where(np.arctan2(-z, x) < 0, WEST, EAST)
    root = np.where(sine > SIN_JOIN_MIDDLE, NORTH, np.where(sine < -SIN_JOIN_MIDDLE, SOUTH, root))
    u = np.zeros(np.shape(x))
    v = np.zeros(np.shape(x))
    for candidate in ROOTS:
        mask = root == candidate
        if np.any(mask):
            cu, cv = uv_on(candidate, vectors)
            u = np.where(mask, cu, u)
            v = np.where(mask, cv, v)
    return root, np.clip(u, 0, 1), np.clip(v, 0, 1)


# Disks -----------------------------------------------------------------------


def _disk_bounds(center, extent, body_radius, message):
    """Root and UV bounds of a spherical cap; the cap must stay on one root."""
    center = np.asarray(center, dtype=float)
    root = int(root_uv(center)[0])
    # A ring around the disk: its edge is where the bounds lie.
    helper = np.array([0.0, 0.0, 1.0]) if abs(center[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    east = np.cross(helper, center)
    east /= np.linalg.norm(east)
    north = np.cross(center, east)
    angle = extent / body_radius
    around = np.linspace(0, 2 * math.pi, 180, endpoint=False)
    ring = math.cos(angle) * center[:, None] + math.sin(angle) * (
        east[:, None] * np.cos(around) + north[:, None] * np.sin(around)
    )
    u, v = uv_on(root, ring)
    if is_cap(root):
        polar = abs(ring[1]) > SIN_JOIN_MIDDLE
    else:
        polar = abs(ring[1]) <= SIN_JOIN_MIDDLE
    if not polar.all() or not (u.min() > 0 and u.max() < 1 and v.min() > 0 and v.max() < 1):
        raise RuntimeError(message)
    return root, (float(u.min()), float(u.max())), (float(v.min()), float(v.max()))


def _disk_tiles(root, us, vs, levels):
    tiles = []
    for level in levels:
        count = 1 << level
        xs = range(math.floor(us[0] * count), math.floor(us[1] * count) + 1)
        ys = range(math.floor(vs[0] * count), math.floor(vs[1] * count) + 1)
        if len(tiles) + len(xs) * len(ys) > MAX_REGION_TILES:
            raise RuntimeError(
                f"Region exceeds {MAX_REGION_TILES:,} tiles per layer; lower level or radius"
            )
        tiles.extend(Tile(root, level, x, y) for y in ys for x in xs)
    return tiles


class Coverage:
    """A detail disk, optionally inside a larger overview disk on the same root.

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
        if not base_level < max_level <= 17:
            raise RuntimeError("Regional max level must exceed global depth and be at most 17")
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
        root, us, vs = _disk_bounds(
            inner, extent, body_radius, "Region crosses a root boundary; reduce its radius"
        )
        first = root_max_level(root, base_level) + 1
        tiles = _disk_tiles(root, us, vs, range(first, root_max_level(root, max_level) + 1))
        self.root = root
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
                self.center, self.radius, body_radius, "Overview crosses a root boundary"
            )
            if outer_face != root:
                raise RuntimeError("Overview and detail must share one root")
            tiles += _disk_tiles(root, ous, ovs, range(first, root_max_level(root, self.level) + 1))
            tiles = sorted(set(tiles), key=lambda t: (t.level, t.root, t.y, t.x))
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
