"""The published documents: the contract with the globe.

Three JSON documents are published: an imagery manifest, a regional
catalog over one imagery manifest, and a height manifest. This module builds
their fixed parts, says which is which, lists the files each references, and
validates them with the same rules as the globe's parsers
(`globes/src/imagery/manifest.ts`, `imagery/regions.ts`, `elevation/tiles.ts`,
`tiles/tile-source.ts`), so a release never contains what the globe rejects.
"""

import math
import re
from pathlib import Path

from tile_service.cube import HEIGHT_MAX, HEIGHT_MIN, HEIGHT_OFFSET, HEIGHT_SCALE

PROFILE = "normalized-cube-v1"
HASHED_NAME = re.compile(r".*-([a-f0-9]{64})")
KEY = re.compile(r"^[0-5]/\d+/\d+/\d+$")
SITE_ID = re.compile(r"^[a-z0-9-]+$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
ARCHIVE_KEY = re.compile(r"^(?:[a-zA-Z0-9_-]+/)*[0-5]$")
ARCHIVE_PATH = re.compile(r"^(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.pmtiles$")
HEIGHT_PATH = re.compile(r"^([a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.u16$")
PAGE_PATH = re.compile(r"^([a-zA-Z0-9_-]+/)*[a-zA-Z0-9_-]+\.json$")

# Fixed conventions every imagery manifest carries; the globe checks them.
IMAGERY_CONVENTIONS = {
    "version": 1,
    "body": "Moon",
    "projection": "normalized-cube",
    "faces": [
        {"id": 0, "name": "+X", "normal": [1, 0, 0], "uAxis": [0, 0, -1], "vAxis": [0, 1, 0]},
        {"id": 1, "name": "-X", "normal": [-1, 0, 0], "uAxis": [0, 0, 1], "vAxis": [0, 1, 0]},
        {"id": 2, "name": "+Y", "normal": [0, 1, 0], "uAxis": [1, 0, 0], "vAxis": [0, 0, -1]},
        {"id": 3, "name": "-Y", "normal": [0, -1, 0], "uAxis": [1, 0, 0], "vAxis": [0, 0, 1]},
        {"id": 4, "name": "+Z", "normal": [0, 0, 1], "uAxis": [1, 0, 0], "vAxis": [0, 1, 0]},
        {"id": 5, "name": "-Z", "normal": [0, 0, -1], "uAxis": [-1, 0, 0], "vAxis": [0, 1, 0]},
    ],
    "axes": {"north": "+Y", "longitude0": "+X", "longitude90East": "-Z"},
    "addressing": {
        "origin": "bottom-left in face-local UV",
        "yDirection": "up",
        "template": "{face}/{level}/{x}/{y}.png",
    },
    "imageRowOrder": "top-to-bottom",
    "textureFlipY": True,
    "minLevel": 0,
    "format": "png",
    "channels": "RGBA",
    "noData": "alpha=0; retain coarser imagery underneath",
    "colorSpace": "srgb",
    "resampling": "average",
    "sampler": {
        "minFilter": "linear",
        "magFilter": "linear",
        "generateMipmaps": False,
        "anisotropy": 1,
        "wrapS": "clamp-to-edge",
        "wrapT": "clamp-to-edge",
    },
}

# Catalog fields that must match the base imagery manifest.
CATALOG_BASE_KEYS = ("baseId", "baseMaxLevel", "tileSize", "gutter")


# Building ----------------------------------------------------------------------


def imagery_manifest(
    dataset, *, radius, tile_size, gutter, max_level, source, generation, toolchain
):
    """An imagery manifest: conventions, dataset description, geometry and provenance."""
    manifest = dict(IMAGERY_CONVENTIONS)
    for key in ("id", "title", "coverage", "nativeResolutionMeters", "quantity"):
        manifest[key] = dataset.get(key)
    image = tile_size + 2 * gutter
    manifest.update(
        {
            "radiusMeters": radius,
            "body": dataset.get("body") or "Moon",
            "tileSize": tile_size,
            "gutter": gutter,
            "imageSize": image,
            "maxLevel": max_level,
            "tileCount": 2 * ((1 << (2 * (max_level + 1))) - 1),
            "textureUV": {"offset": gutter / image, "scale": tile_size / image},
            "faceCenterMetersPerPixelAtMaxLevel": 2 * radius / (tile_size * (1 << max_level)),
            "source": source,
            "pipelineVersion": toolchain["pipelineVersion"],
            "toolchain": {"implementation": "Python", "gdal": toolchain["gdal"]},
            "generation": generation,
        }
    )
    return manifest


def regional_catalog(base):
    """An empty regional catalog over `base`, an imagery manifest."""
    return {
        "version": 1,
        "body": base.get("body"),
        "radiusMeters": float(base["radiusMeters"]),
        "baseId": base.get("id"),
        "baseMaxLevel": int(base["maxLevel"]),
        "tileSize": int(base["tileSize"]),
        "gutter": int(base["gutter"]),
        "sites": [],
    }


def height_manifest(*, body, radius, tile_size, gutter, max_level):
    """An empty height manifest; `HeightIndex` fills and publishes it."""
    return {
        "version": 1,
        "body": body,
        "radiusMeters": radius,
        "projection": "normalized-cube",
        "format": "uint16-le",
        "scaleMeters": HEIGHT_SCALE,
        "offsetMeters": HEIGHT_OFFSET,
        "sources": [],
        "tileSize": tile_size,
        "gutter": gutter,
        "maxLevel": max_level,
        "tiles": {},
    }


# Reading -------------------------------------------------------------------


def kind(document):
    """ "imagery", "heights", "regions", or None for anything else."""
    if "sites" in document:
        return "regions"
    if document.get("projection") == "normalized-cube":
        return "heights" if document.get("format") == "uint16-le" else "imagery"
    return None


def assets(document):
    """Files a packed document references, relative to its directory, with expected SHA-256."""
    found = []

    def archives(prefix, storage):
        if not storage:
            raise RuntimeError(f"Dataset is not packed; run pack first: {prefix or '.'}")
        if storage.get("type") != "pmtiles" or storage.get("profile") != PROFILE:
            raise RuntimeError("Unsupported archive storage descriptor")
        for name in storage["archives"].values():
            path = Path(prefix) / name
            match = HASHED_NAME.fullmatch(path.stem)
            if not match:
                raise RuntimeError(f"Archive needs a content hash: {path}")
            found.append((path, match.group(1)))

    if kind(document) == "regions":
        for site in document["sites"]:
            archives(site["id"], site.get("storage"))
    else:
        archives("", document.get("storage"))
        found += [(Path(p["path"]), p["sha256"]) for p in document.get("pages", {}).values()]
    return found


# Validating ----------------------------------------------------------------


class InvalidDocument(RuntimeError):
    pass


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(value):
    return _number(value) and float(value).is_integer()


def _require(condition, message):
    if not condition:
        raise InvalidDocument(message)


def _body(document):
    return (
        isinstance(document.get("body"), str)
        and document["body"].strip() != ""
        and _number(document.get("radiusMeters"))
        and document["radiusMeters"] > 0
    )


def validate_storage(storage):
    if storage is None:
        return
    archives = storage.get("archives") if isinstance(storage, dict) else None
    _require(
        isinstance(storage, dict)
        and storage.get("type") == "pmtiles"
        and storage.get("profile") == PROFILE
        and isinstance(archives, dict)
        and 0 < len(archives) <= 4096,
        "Invalid PMTiles storage descriptor.",
    )
    for key, path in archives.items():
        _require(
            ARCHIVE_KEY.match(key) and isinstance(path, str) and ARCHIVE_PATH.match(path),
            "Invalid PMTiles archive reference.",
        )


def validate_imagery(m):
    size, gutter = m.get("tileSize"), m.get("gutter")
    image, uv = m.get("imageSize"), m.get("textureUV") or {}
    axes, addressing = m.get("axes") or {}, m.get("addressing") or {}
    _require(
        m.get("version") == 1
        and _body(m)
        and m.get("projection") == "normalized-cube"
        and axes.get("north") == "+Y"
        and axes.get("longitude0") == "+X"
        and axes.get("longitude90East") == "-Z"
        and addressing.get("template") == "{face}/{level}/{x}/{y}.png"
        and addressing.get("yDirection") == "up"
        and m.get("textureFlipY") is True
        and m.get("format") == "png"
        and m.get("colorSpace") == "srgb"
        and m.get("minLevel") == 0
        and _integer(m.get("maxLevel"))
        and 0 <= m["maxLevel"] <= 20
        and _integer(size)
        and 1 <= size <= 2048
        and _integer(gutter)
        and 1 <= gutter <= 16
        and image == size + 2 * gutter
        and uv.get("offset") == gutter / image
        and uv.get("scale") == size / image,
        "Unsupported globe imagery manifest or coordinate convention.",
    )
    validate_storage(m.get("storage"))


def validate_regions(c, base):
    sites = c.get("sites")
    _require(
        c.get("version") == 1
        and c.get("body") == base.get("body")
        and c.get("radiusMeters") == base.get("radiusMeters")
        and c.get("baseId") == base.get("id")
        and c.get("baseMaxLevel") == base.get("maxLevel")
        and c.get("tileSize") == base.get("tileSize")
        and c.get("gutter") == base.get("gutter")
        and isinstance(sites, list)
        and len(sites) <= 64,
        "Incompatible regional imagery catalog.",
    )
    ids, addresses = set(), set()
    for site in sites:
        source = site.get("source") if isinstance(site, dict) else None
        _require(
            isinstance(site, dict)
            and isinstance(site.get("id"), str)
            and SITE_ID.match(site["id"])
            and site["id"] not in ids
            and isinstance(site.get("name"), str)
            and _number(site.get("latitudeDeg"))
            and abs(site["latitudeDeg"]) <= 90
            and _number(site.get("longitudeDeg"))
            and abs(site["longitudeDeg"]) <= 180
            and _number(site.get("radiusMeters"))
            and site["radiusMeters"] > 0
            and _integer(site.get("maxLevel"))
            and base["maxLevel"] < site["maxLevel"] <= 16
            and isinstance(site.get("tiles"), list)
            and len(site["tiles"]) <= 20000
            and isinstance(source, dict)
            and isinstance(source.get("sourcePage"), str)
            and isinstance(source.get("credit"), str)
            and _number(source.get("nativeResolutionMeters"))
            and source["nativeResolutionMeters"] > 0,
            f"Invalid regional imagery site: {site.get('id') if isinstance(site, dict) else site}",
        )
        validate_storage(site.get("storage"))
        ids.add(site["id"])
        own = set()
        for key in site["tiles"]:
            _require(isinstance(key, str) and KEY.match(key), "Invalid regional tile address.")
            face, level, x, y = (int(part) for part in key.split("/"))
            _require(
                base["maxLevel"] < level <= site["maxLevel"]
                and x < 2**level
                and y < 2**level
                and key == f"{face}/{level}/{x}/{y}"
                and key not in addresses,
                "Invalid or overlapping regional tile address.",
            )
            addresses.add(key)
            own.add(key)
        # A complete ancestor chain keeps progressive loading and fades intact.
        for key in own:
            face, level, x, y = (int(part) for part in key.split("/"))
            if level > base["maxLevel"] + 1:
                _require(
                    f"{face}/{level - 1}/{x >> 1}/{y >> 1}" in own,
                    "Regional tile is missing its parent.",
                )


def validate_heights(m):
    tiles = m.get("tiles")
    _require(
        m.get("version") == 1
        and _body(m)
        and m.get("projection") == "normalized-cube"
        and m.get("format") == "uint16-le"
        and m.get("scaleMeters") == HEIGHT_SCALE
        and m.get("offsetMeters") == HEIGHT_OFFSET
        and _integer(m.get("tileSize"))
        and 16 <= m["tileSize"] <= 256
        and _integer(m.get("gutter"))
        and 2 <= m["gutter"] <= 8
        and _integer(m.get("maxLevel"))
        and 0 <= m["maxLevel"] <= 20
        and _number(m.get("minMeters"))
        and _number(m.get("maxMeters"))
        and m["minMeters"] >= HEIGHT_MIN
        and m["maxMeters"] <= HEIGHT_MAX
        and m["minMeters"] <= m["maxMeters"]
        and isinstance(tiles, dict)
        and isinstance(m.get("sources"), list),
        "Unsupported height tile manifest.",
    )
    _require(len(tiles) <= 200000, "Height catalog exceeds metadata budget.")
    for face in range(6):
        _require(f"{face}/0/0/0" in tiles, "Height catalog requires all six roots.")
    for key, t in tiles.items():
        _require(KEY.match(key), "Invalid height tile address.")
        f, level, x, y = (int(part) for part in key.split("/"))
        _require(
            level <= m["maxLevel"]
            and x < 2**level
            and y < 2**level
            and key == f"{f}/{level}/{x}/{y}"
            and isinstance(t, dict)
            and isinstance(t.get("path"), str)
            and HEIGHT_PATH.match(t["path"])
            and all(_number(t.get(k)) for k in ("min", "max", "lower", "upper"))
            and t["min"] <= t["max"]
            and t["lower"] <= t["min"]
            and t["upper"] >= t["max"]
            and t["lower"] >= m["minMeters"]
            and t["upper"] <= m["maxMeters"]
            and isinstance(t.get("sha256"), str)
            and SHA256.match(t["sha256"]),
            f"Invalid height tile metadata: {key}",
        )
        if level > 0:
            parent = tiles.get(f"{f}/{level - 1}/{x >> 1}/{y >> 1}")
            _require(
                parent is not None
                and parent["lower"] <= t["lower"]
                and parent["upper"] >= t["upper"],
                f"Nonconservative height hierarchy: {key}",
            )
    pages = m.get("pages")
    if pages:
        _require(
            _integer(m.get("pageLevel")) and 0 <= m["pageLevel"] <= 5 and len(pages) <= 8192,
            "Invalid height page index.",
        )
        for key, page in pages.items():
            parent = tiles.get(key)
            _require(
                parent is not None
                and int(key.split("/")[1]) == m["pageLevel"]
                and isinstance(page, dict)
                and isinstance(page.get("path"), str)
                and PAGE_PATH.match(page["path"])
                and isinstance(page.get("sha256"), str)
                and SHA256.match(page["sha256"])
                and _number(page.get("lower"))
                and _number(page.get("upper"))
                and page["lower"] >= parent["lower"]
                and page["upper"] <= parent["upper"]
                and page["lower"] <= page["upper"],
                f"Invalid height metadata page: {key}",
            )
    validate_storage(m.get("storage"))


def validate(document, base=None):
    """Raise InvalidDocument unless the globe would accept `document`.

    A regional catalog needs `base`, the imagery manifest it refines.
    """
    found = kind(document)
    if found == "imagery":
        validate_imagery(document)
    elif found == "heights":
        validate_heights(document)
    elif found == "regions":
        if base is None:
            raise InvalidDocument("A regional catalog is validated against its base manifest")
        validate_regions(document, base)
    else:
        raise InvalidDocument("Not a published tile-service document")
