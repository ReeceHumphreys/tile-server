"""Writes grid.json and documents.json next to this file. Standard library only, and from
the formulas in ../README.md, so the values don't come from any tool they will check."""

import json
import math
from pathlib import Path

HERE = Path(__file__).parent
CAP_HALF = math.pi / 32


def position(root, u, v):
    """Latitude and longitude, in degrees, of a root's u and v."""
    if root < 2:
        return (v - 0.5) * 180, (u - 1 + root) * 180
    x, y = (2 * u - 1) * CAP_HALF, (2 * v - 1) * CAP_HALF
    colatitude = math.degrees(2 * math.atan(math.hypot(x, y)))
    longitude = math.degrees(math.atan2(x, -y if root == 2 else y))
    return (90 - colatitude if root == 2 else colatitude - 90), longitude


def grid():
    fractions = (0, 0.125, 0.25, 0.5, 0.6, 0.875, 1)
    points = []
    for root in range(4):
        for u in fractions:
            for v in fractions:
                latitude, longitude = position(root, u, v)
                points.append(
                    {"root": root, "u": u, "v": v, "latitude": latitude, "longitude": longitude}
                )
    levels = [
        {"root": root, "maxLevel": level, "rootMaxLevel": level if root < 2 else max(0, level - 3)}
        for root in range(4)
        for level in (0, 2, 3, 4, 8, 20)
    ]
    pixels = []
    for root, level, x, y, size, gutter in (
        (0, 0, 0, 0, 256, 2),
        (1, 3, 5, 2, 256, 2),
        (2, 2, 3, 0, 128, 4),
        (3, 5, 17, 30, 16, 1),
    ):
        width = size + 2 * gutter
        for col, row in ((0, 0), (gutter, gutter), (width - 1, 0), (width // 2, width - 1)):
            pixels.append(
                {
                    "root": root,
                    "level": level,
                    "x": x,
                    "y": y,
                    "tileSize": size,
                    "gutter": gutter,
                    "col": col,
                    "row": row,
                    "u": (x + (col - gutter + 0.5) / size) / 2**level,
                    "v": (y + 1 - (row - gutter + 0.5) / size) / 2**level,
                }
            )
    heights = [
        {
            "stored": stored,
            "scaleMeters": scale,
            "offsetMeters": offset,
            "meters": stored * scale + offset,
        }
        for stored, scale, offset in (
            (0, 0.5, -10000),
            (20000, 0.5, -10000),
            (65535, 0.5, -10000),
            (0, 0.5, -11000),
            (22000, 0.5, -11000),
            (1234, 0.25, 100),
        )
    ]
    return {"points": points, "levels": levels, "pixels": pixels, "heights": heights}


HASH = "a" * 64
ARCHIVE = {
    "type": "pmtiles",
    "profile": "geographic-polar-v1",
    "archives": {"0": "pmtiles/0.pmtiles"},
}
IMAGERY = {
    "version": 2,
    "id": "sample",
    "body": "Moon",
    "radiusMeters": 1737400,
    "projection": "geographic-polar",
    "format": "png",
    "colorSpace": "srgb",
    "tileSize": 256,
    "gutter": 2,
    "maxLevel": 4,
}


def tile(path, low=0, high=10):
    return {"path": path, "sha256": HASH, "min": low, "max": high, "lower": low, "upper": high}


TILES = {f"{root}/0/0/0": tile(f"global/{root}/0/0/0.u16") for root in range(4)}
HEIGHTS = {
    "version": 2,
    "body": "Moon",
    "radiusMeters": 1737400,
    "projection": "geographic-polar",
    "format": "uint16-le",
    "scaleMeters": 0.5,
    "offsetMeters": -10000,
    "verticalDatum": "sphere",
    "tileSize": 128,
    "gutter": 2,
    "maxLevel": 4,
    "minMeters": 0,
    "maxMeters": 10,
    "sources": [{"id": "global", "credit": "Sample", "sourcePage": "https://example.test/"}],
    "tiles": TILES,
}
SITE = {
    "id": "site",
    "name": "Sample",
    "latitudeDeg": 10,
    "longitudeDeg": 20,
    "radiusMeters": 2000,
    "maxLevel": 6,
    "source": {
        "sourcePage": "https://example.test/",
        "credit": "Sample",
        "nativeResolutionMeters": 1,
    },
    "tiles": ["1/5/3/20", "1/6/6/40"],
}
CATALOG = {
    "version": 1,
    "body": "Moon",
    "radiusMeters": 1737400,
    "baseId": "sample",
    "baseMaxLevel": 4,
    "tileSize": 256,
    "gutter": 2,
    "sites": [SITE],
}


def without(document, key):
    return {name: value for name, value in document.items() if name != key}


def documents():
    cases = []

    def case(name, kind, document, valid, base=None):
        entry = {"name": name, "type": kind, "document": document, "valid": valid}
        if kind == "regions":
            entry["base"] = base or IMAGERY
        cases.append(entry)

    # Imagery.
    case("imagery: smallest valid manifest", "imagery", IMAGERY, True)
    case("imagery: unknown fields are ignored", "imagery", {**IMAGERY, "future": {"a": 1}}, True)
    case("imagery: packed", "imagery", {**IMAGERY, "storage": ARCHIVE}, True)
    for field, value in (
        ("tileSize", 1),
        ("tileSize", 2048),
        ("gutter", 1),
        ("gutter", 16),
        ("maxLevel", 0),
        ("maxLevel", 20),
    ):
        case(f"imagery: {field} at its limit {value}", "imagery", {**IMAGERY, field: value}, True)
    for field in (
        "version",
        "id",
        "body",
        "radiusMeters",
        "projection",
        "format",
        "colorSpace",
        "tileSize",
        "gutter",
        "maxLevel",
    ):
        case(f"imagery: missing {field}", "imagery", without(IMAGERY, field), False)
    for field, value in (
        ("version", 1),
        ("version", 3),
        ("id", ""),
        ("id", 1),
        ("body", " "),
        ("radiusMeters", 0),
        ("radiusMeters", "1737400"),
        ("projection", "cube"),
        ("format", "jpeg"),
        ("colorSpace", "linear"),
        ("tileSize", 0),
        ("tileSize", 2049),
        ("tileSize", 1.5),
        ("gutter", 0),
        ("gutter", 17),
        ("maxLevel", -1),
        ("maxLevel", 21),
        ("maxLevel", "4"),
    ):
        case(f"imagery: {field} is {value!r}", "imagery", {**IMAGERY, field: value}, False)

    for field in ("title", "coverage", "quantity", "nativeResolutionMeters", "source", "producer"):
        case(f"imagery: {field} is null", "imagery", {**IMAGERY, field: None}, False)

    # Storage.
    case("storage: null is not absent", "imagery", {**IMAGERY, "storage": None}, False)
    dotted = {**ARCHIVE, "archives": {"v1.2/0": "v1.2/0-a.b.pmtiles"}}
    case("storage: dots inside a path", "imagery", {**IMAGERY, "storage": dotted}, True)
    for name, storage in (
        ("wrong type", {**ARCHIVE, "type": "mbtiles"}),
        ("wrong profile", {**ARCHIVE, "profile": "x"}),
        ("no archives", {**ARCHIVE, "archives": {}}),
        ("root out of range", {**ARCHIVE, "archives": {"4": "a.pmtiles"}}),
        ("path escapes", {**ARCHIVE, "archives": {"0": "../a.pmtiles"}}),
        ("hidden path part", {**ARCHIVE, "archives": {"0": ".a/b.pmtiles"}}),
        ("absolute path", {**ARCHIVE, "archives": {"0": "/a.pmtiles"}}),
        ("URL as a path", {**ARCHIVE, "archives": {"0": "https:a.pmtiles"}}),
        ("path is not an archive", {**ARCHIVE, "archives": {"0": "a.png"}}),
    ):
        case(f"storage: {name}", "imagery", {**IMAGERY, "storage": storage}, False)

    # Heights.
    deep = {**TILES, "2/1/0/0": tile("global/2/1/0/0.u16")}
    case("heights: smallest valid manifest", "heights", HEIGHTS, True)
    case("heights: packed", "heights", {**HEIGHTS, "storage": ARCHIVE}, True)
    case(
        "heights: dots inside a path",
        "heights",
        {**HEIGHTS, "tiles": {**TILES, "0/0/0/0": tile("v1.2/0/0/0/0.u16")}},
        True,
    )
    case("heights: empty pages", "heights", {**HEIGHTS, "pageLevel": 6, "pages": {}}, True)
    case("heights: a cap at its deepest level", "heights", {**HEIGHTS, "tiles": deep}, True)
    case(
        "heights: other encoding",
        "heights",
        {**HEIGHTS, "scaleMeters": 0.25, "offsetMeters": -5, "verticalDatum": "ellipsoid"},
        True,
    )
    for field, value in (("tileSize", 16), ("tileSize", 256), ("gutter", 2), ("gutter", 8)):
        case(f"heights: {field} at its limit {value}", "heights", {**HEIGHTS, field: value}, True)
    case(
        "heights: paged",
        "heights",
        {
            **HEIGHTS,
            "pageLevel": 0,
            "pages": {
                "0/0/0/0": {"path": "metadata/0.json", "sha256": HASH, "lower": 0, "upper": 10}
            },
        },
        True,
    )
    for field in (
        "version",
        "body",
        "radiusMeters",
        "projection",
        "format",
        "scaleMeters",
        "offsetMeters",
        "verticalDatum",
        "tileSize",
        "gutter",
        "maxLevel",
        "minMeters",
        "maxMeters",
        "sources",
        "tiles",
    ):
        case(f"heights: missing {field}", "heights", without(HEIGHTS, field), False)
    for field, value in (
        ("scaleMeters", 0),
        ("scaleMeters", "0.5"),
        ("offsetMeters", "0"),
        ("verticalDatum", 1),
        ("tileSize", 15),
        ("tileSize", 257),
        ("gutter", 1),
        ("gutter", 9),
        ("maxLevel", 21),
        ("minMeters", -20000),
        ("maxMeters", 30000),
        ("sources", {}),
    ):
        case(f"heights: {field} is {value!r}", "heights", {**HEIGHTS, field: value}, False)
    case("heights: minMeters above maxMeters", "heights", {**HEIGHTS, "minMeters": 11}, False)
    case("storage: null in heights", "heights", {**HEIGHTS, "storage": None}, False)
    for name, sources in (
        ("a source that isn't an object", [1]),
        ("a source missing its fields", [{}]),
        ("a source with a null credit", [{"id": "a", "credit": None, "sourcePage": ""}]),
    ):
        case(f"heights: {name}", "heights", {**HEIGHTS, "sources": sources}, False)
    case(
        "heights: every root is required",
        "heights",
        {**HEIGHTS, "tiles": without(TILES, "3/0/0/0")},
        False,
    )
    for name, entries in (
        ("a tile needs its parent", {**TILES, "0/2/0/0": tile("a/0/2/0/0.u16")}),
        (
            "a tile stays inside its parent",
            {**TILES, "0/1/0/0": {**tile("a/0/1/0/0.u16"), "upper": 11}},
        ),
        (
            "a geographic tile deeper than maxLevel",
            {**TILES, **{f"0/{n}/0/0": tile(f"a/0/{n}/0/0.u16") for n in range(1, 6)}},
        ),
        (
            "a cap tile deeper than its root allows",
            {**TILES, **{f"2/{n}/0/0": tile(f"a/2/{n}/0/0.u16") for n in range(1, 3)}},
        ),
        ("an address past the root's edge", {**TILES, "0/1/2/0": tile("a/0/1/2/0.u16")}),
        ("a noncanonical address", {**TILES, "0/01/0/0": tile("a/0/1/0/0.u16")}),
        ("a bad path", {**TILES, "0/1/0/0": tile("a/0/1/0/0.png")}),
        ("a bad hash", {**TILES, "0/1/0/0": {**tile("a/0/1/0/0.u16"), "sha256": "abc"}}),
        ("min above max", {**TILES, "0/1/0/0": {**tile("a/0/1/0/0.u16"), "min": 9, "max": 8}}),
    ):
        case(f"heights: {name}", "heights", {**HEIGHTS, "tiles": entries}, False)
    page = {"path": "metadata/0.json", "sha256": HASH, "lower": 0, "upper": 10}
    for name, fields in (
        ("pageLevel above 6", {"pageLevel": 7, "pages": {"0/0/0/0": page}}),
        ("pages is null", {"pageLevel": 0, "pages": None}),
        ("pageLevel is null", {"pageLevel": None, "pages": {}}),
        ("pageLevel without pages", {"pageLevel": 0}),
        ("pages without pageLevel", {"pages": {}}),
        (
            "a page path that isn't JSON",
            {"pageLevel": 0, "pages": {"0/0/0/0": {**page, "path": "a.u16"}}},
        ),
        ("a page at the wrong level", {"pageLevel": 1, "pages": {"0/0/0/0": page}}),
        ("a page without its tile", {"pageLevel": 1, "pages": {"0/1/0/0": page}}),
        ("a page outside its tile", {"pageLevel": 0, "pages": {"0/0/0/0": {**page, "upper": 11}}}),
    ):
        case(f"heights: {name}", "heights", {**HEIGHTS, **fields}, False)

    # Regional catalogs. The base reaches dataset level 4, so the caps stop at root level 1.
    def sites(*changed):
        return {**CATALOG, "sites": list(changed)}

    cap = {**SITE, "id": "cap", "latitudeDeg": 85, "tiles": ["2/2/1/1", "2/3/2/2"]}
    case("regions: a site", "regions", CATALOG, True)
    case("regions: no sites", "regions", sites(), True)
    case("regions: a cap site", "regions", sites(cap), True)
    case("regions: a packed site", "regions", sites({**SITE, "storage": ARCHIVE}), True)
    case("regions: site maxLevel at its limit", "regions", sites({**SITE, "maxLevel": 17}), True)
    for field, value in (
        ("version", 2),
        ("body", "Mars"),
        ("radiusMeters", 1),
        ("baseId", "x"),
        ("baseMaxLevel", 3),
        ("tileSize", 128),
        ("gutter", 1),
        ("sites", {}),
    ):
        case(f"regions: {field} is {value!r}", "regions", {**CATALOG, field: value}, False)
    case(
        "regions: more sites than allowed",
        "regions",
        sites(*({**SITE, "id": f"s{n}", "tiles": []} for n in range(65))),
        False,
    )
    for field in (
        "id",
        "name",
        "latitudeDeg",
        "longitudeDeg",
        "radiusMeters",
        "maxLevel",
        "source",
        "tiles",
    ):
        case(f"regions: site missing {field}", "regions", sites(without(SITE, field)), False)
    for field, value in (
        ("id", "Not Valid"),
        ("latitudeDeg", 91),
        ("longitudeDeg", 181),
        ("radiusMeters", 0),
        ("maxLevel", 4),
        ("maxLevel", 18),
        ("source", {}),
    ):
        case(f"regions: site {field} is {value!r}", "regions", sites({**SITE, field: value}), False)
    case("storage: null in a site", "regions", sites({**SITE, "storage": None}), False)
    case(
        "regions: repeated site id",
        "regions",
        sites({**SITE, "tiles": []}, {**SITE, "tiles": []}),
        False,
    )
    case("regions: a tile in two sites", "regions", sites(SITE, {**SITE, "id": "other"}), False)
    for name, tiles in (
        ("a tile no deeper than the base", ["1/4/1/10"]),
        ("a tile deeper than the site", ["1/5/3/20", "1/6/6/40", "1/7/12/80"]),
        ("a tile missing its parent", ["1/6/6/40"]),
        ("a bad address", ["1/5/3"]),
        ("an address past the root's edge", ["1/5/32/0"]),
        ("a noncanonical address", ["1/05/3/20"]),
    ):
        case(f"regions: {name}", "regions", sites({**SITE, "tiles": tiles}), False)
    # Cap root levels run three behind, so these all pass a check against dataset levels.
    case(
        "regions: a cap tile deeper than the site on its root",
        "regions",
        sites({**cap, "tiles": ["2/2/1/1", "2/3/2/2", "2/4/4/4"]}),
        False,
    )
    case(
        "regions: a cap tile no deeper than the base on its root",
        "regions",
        sites({**cap, "tiles": ["2/1/0/0"]}),
        False,
    )
    return cases


def write(name, value):
    (HERE / name).write_text(json.dumps(value, indent=1) + "\n")


if __name__ == "__main__":
    write("grid.json", grid())
    write("documents.json", documents())
