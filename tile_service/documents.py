"""The published documents.

Three JSON documents are published: an imagery manifest, a regional catalog over one imagery
manifest, and a height manifest. This module builds their fixed parts, says which is which,
lists the files each references, and validates them against `spec/README.md`, so a release
never contains what the contract forbids. `spec/conformance/documents.json` checks it.
"""

import math
import re
from pathlib import Path

from tile_service.roots import ROOTS, root_max_level

PROFILE = "geographic-polar-v1"
HASHED_NAME = re.compile(r".*-([a-f0-9]{64})")
KEY = re.compile(r"^[0-3]/\d+/\d+/\d+$")
SITE_ID = re.compile(r"^[a-z0-9-]+$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
PATH_PART = r"[a-zA-Z0-9_-][a-zA-Z0-9._-]*"
ARCHIVE_KEY = re.compile(rf"^(?:{PATH_PART}/)*[0-3]$")
ARCHIVE_PATH = re.compile(rf"^(?:{PATH_PART}/)*{PATH_PART}\.pmtiles$")
HEIGHT_PATH = re.compile(rf"^(?:{PATH_PART}/)*{PATH_PART}\.u16$")
PAGE_PATH = re.compile(rf"^(?:{PATH_PART}/)*{PATH_PART}\.json$")

# The contract's limits.
MAX_LEVEL = 20
MAX_SITE_LEVEL = 17
MAX_IMAGE_TILE_SIZE = 2048
MAX_IMAGE_GUTTER = 16
HEIGHT_TILE_SIZES = (16, 256)
HEIGHT_GUTTERS = (2, 8)
MAX_HEIGHT_TILES = 200000
MAX_PAGE_LEVEL = 6
MAX_PAGES = 8192
MAX_ARCHIVES = 4096
MAX_SITES = 64
MAX_SITE_TILES = 20000

# Optional fields only readers of people's text use. Optional means absent, not null.
DESCRIPTION = ("title", "coverage", "quantity", "nativeResolutionMeters", "source", "producer")

# Catalog fields that must match the base imagery manifest.
CATALOG_BASE_KEYS = ("baseId", "baseMaxLevel", "tileSize", "gutter")


# Building ----------------------------------------------------------------------


def imagery_manifest(dataset, *, radius, tile_size, gutter, max_level, source, producer):
    """An imagery manifest. `producer` is this tool's own record; readers ignore it."""
    described = ("title", "coverage", "nativeResolutionMeters", "quantity")
    return {
        "version": 2,
        "id": dataset["id"],
        "body": dataset["body"],
        "radiusMeters": radius,
        "projection": "geographic-polar",
        "format": "png",
        "colorSpace": "srgb",
        "tileSize": tile_size,
        "gutter": gutter,
        "maxLevel": max_level,
        **{key: dataset[key] for key in described if dataset.get(key) is not None},
        "source": source,
        "producer": producer,
    }


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


def height_manifest(*, body, radius, tile_size, gutter, max_level, scale, offset, vertical_datum):
    """An empty height manifest; `HeightIndex` fills and publishes it."""
    return {
        "version": 2,
        "body": body,
        "radiusMeters": radius,
        "projection": "geographic-polar",
        "format": "uint16-le",
        "scaleMeters": scale,
        "offsetMeters": offset,
        "verticalDatum": vertical_datum,
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
    if document.get("projection") == "geographic-polar":
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


def _validate_storage(owner):
    """`storage` is optional, and optional means absent: null is invalid."""
    if "storage" not in owner:
        return
    storage = owner["storage"]
    archives = storage.get("archives") if isinstance(storage, dict) else None
    _require(
        isinstance(storage, dict)
        and storage.get("type") == "pmtiles"
        and storage.get("profile") == PROFILE
        and isinstance(archives, dict)
        and 0 < len(archives) <= MAX_ARCHIVES,
        "Invalid PMTiles storage descriptor.",
    )
    for key, path in archives.items():
        _require(
            ARCHIVE_KEY.match(key) and isinstance(path, str) and ARCHIVE_PATH.match(path),
            "Invalid PMTiles archive reference.",
        )


def validate_imagery(m):
    size, gutter = m.get("tileSize"), m.get("gutter")
    _require(
        m.get("version") == 2
        and isinstance(m.get("id"), str)
        and m["id"] != ""
        and _body(m)
        and m.get("projection") == "geographic-polar"
        and m.get("format") == "png"
        and m.get("colorSpace") == "srgb"
        and _integer(m.get("maxLevel"))
        and 0 <= m["maxLevel"] <= MAX_LEVEL
        and _integer(size)
        and 1 <= size <= MAX_IMAGE_TILE_SIZE
        and _integer(gutter)
        and 1 <= gutter <= MAX_IMAGE_GUTTER
        and all(m.get(key, True) is not None for key in DESCRIPTION),
        "Unsupported imagery manifest.",
    )
    _validate_storage(m)


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
        and len(sites) <= MAX_SITES,
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
            and base["maxLevel"] < site["maxLevel"] <= MAX_SITE_LEVEL
            and isinstance(site.get("tiles"), list)
            and len(site["tiles"]) <= MAX_SITE_TILES
            and isinstance(source, dict)
            and isinstance(source.get("sourcePage"), str)
            and isinstance(source.get("credit"), str)
            and _number(source.get("nativeResolutionMeters"))
            and source["nativeResolutionMeters"] > 0,
            f"Invalid regional imagery site: {site.get('id') if isinstance(site, dict) else site}",
        )
        _validate_storage(site)
        ids.add(site["id"])
        own = set()
        for key in site["tiles"]:
            _require(isinstance(key, str) and KEY.match(key), "Invalid regional tile address.")
            root, level, x, y = (int(part) for part in key.split("/"))
            _require(
                root_max_level(root, base["maxLevel"])
                < level
                <= root_max_level(root, site["maxLevel"])
                and x < 2**level
                and y < 2**level
                and key == f"{root}/{level}/{x}/{y}"
                and key not in addresses,
                "Invalid or overlapping regional tile address.",
            )
            addresses.add(key)
            own.add(key)
        # A complete ancestor chain keeps progressive loading and fades intact.
        for key in own:
            root, level, x, y = (int(part) for part in key.split("/"))
            if level > root_max_level(root, base["maxLevel"]) + 1:
                _require(
                    f"{root}/{level - 1}/{x >> 1}/{y >> 1}" in own,
                    "Regional tile is missing its parent.",
                )


def _source(source):
    return isinstance(source, dict) and all(
        isinstance(source.get(key), str) for key in ("id", "credit", "sourcePage")
    )


def validate_heights(m):
    tiles = m.get("tiles")
    _require(
        m.get("version") == 2
        and _body(m)
        and m.get("projection") == "geographic-polar"
        and m.get("format") == "uint16-le"
        and _number(m.get("scaleMeters"))
        and m["scaleMeters"] > 0
        and _number(m.get("offsetMeters"))
        and isinstance(m.get("verticalDatum"), str)
        and _integer(m.get("tileSize"))
        and HEIGHT_TILE_SIZES[0] <= m["tileSize"] <= HEIGHT_TILE_SIZES[1]
        and _integer(m.get("gutter"))
        and HEIGHT_GUTTERS[0] <= m["gutter"] <= HEIGHT_GUTTERS[1]
        and _integer(m.get("maxLevel"))
        and 0 <= m["maxLevel"] <= MAX_LEVEL
        and _number(m.get("minMeters"))
        and _number(m.get("maxMeters"))
        and m["minMeters"] >= m["offsetMeters"]
        and m["maxMeters"] <= 65535 * m["scaleMeters"] + m["offsetMeters"]
        and m["minMeters"] <= m["maxMeters"]
        and isinstance(tiles, dict)
        and isinstance(m.get("sources"), list)
        and all(_source(source) for source in m["sources"]),
        "Unsupported height tile manifest.",
    )
    _require(len(tiles) <= MAX_HEIGHT_TILES, "Height catalog exceeds metadata budget.")
    for root in ROOTS:
        _require(f"{root}/0/0/0" in tiles, "Height catalog requires every root.")
    for key, t in tiles.items():
        _require(KEY.match(key), "Invalid height tile address.")
        f, level, x, y = (int(part) for part in key.split("/"))
        _require(
            level <= root_max_level(f, m["maxLevel"])
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
    if "pages" in m or "pageLevel" in m:
        pages = m.get("pages")
        _require(
            "pages" in m
            and _integer(m.get("pageLevel"))
            and 0 <= m["pageLevel"] <= MAX_PAGE_LEVEL
            and isinstance(pages, dict)
            and len(pages) <= MAX_PAGES,
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
    _validate_storage(m)


def validate(document, base=None):
    """Raise InvalidDocument unless `document` meets the contract.

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
