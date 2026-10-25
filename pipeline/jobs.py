"""Dataset preparation commands; no renderer or JavaScript runtime required."""

import argparse
import math
import shutil
import tempfile
import time
from pathlib import Path

from pipeline.region import Coverage, disk_addresses
from pipeline.runtime import MOON_RADIUS, Args, atomic_json, catalog_path, read_json


def execute(command, *options):
    from pipeline.cli import dispatch

    dispatch(Args(["tile-service", command, *map(str, options)]))


def plan_coverage(detail, overview=None, min_level=1):
    center, addresses = disk_addresses(
        detail["latitudeDeg"],
        detail["longitudeDeg"],
        detail["radiusMeters"],
        MOON_RADIUS,
        min_level - 1,
        detail["maxLevel"],
    )
    if overview:
        coverage = Coverage(
            {"overview": overview},
            center,
            detail["radiusMeters"],
            min_level - 1,
            detail["maxLevel"],
            MOON_RADIUS,
        )
        coverage.append(addresses, min_level - 1, addresses[0].face)
    return addresses


def apollo(argv):
    parser = argparse.ArgumentParser(prog="tile-service apollo")
    parser.add_argument("--site")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--info", action="store_true")
    parser.add_argument("--elevation", action="store_true")
    parser.add_argument("--radius-meters", type=int, default=2000)
    parser.add_argument("--max-level", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base", type=Path)
    args = parser.parse_args(argv)
    catalog = read_json(
        catalog_path("apollo-elevation.json" if args.elevation else "apollo-nac.json")
    )
    if args.list or not args.site:
        parser.print_usage()
        for site in catalog["sites"]:
            print(
                f"{site['id']}: {site['nativeResolutionMeters']} m/px; "
                f"{site['expectedBytes'] / 1024**2:.1f} MiB source TIFF"
            )
        return
    selected = args.site.lower().replace(" ", "").replace("-", "")
    sites = [
        s
        for s in catalog["sites"]
        if selected == "all" or s["id"].replace("-", "") in (selected, selected + "dtm")
    ]
    if not sites:
        raise RuntimeError("Unknown Apollo site; use --list")
    if not 100 <= args.radius_meters <= 20000:
        raise RuntimeError("Radius must be 100..20000 meters")
    if args.max_level is not None and not 1 <= args.max_level <= 16:
        raise RuntimeError("Max level must be 1..16")
    output = args.output or Path(
        "public/data/moon/elevation-tiles" if args.elevation else "public/data/moon/apollo"
    )
    base = args.base or (
        output / "manifest.json" if args.elevation else Path("public/data/moon/wac/manifest.json")
    )
    if not args.info:
        manifest = read_json(base)
        if manifest.get("projection") != "normalized-cube" or manifest.get("body") != "Moon":
            raise RuntimeError("Expected a lunar normalized-cube base manifest")
    for site in sites:
        raw = Path("data/raw/moon/apollo") / (site["id"] + ".tif")
        config = {
            **site,
            "sourceFile": str(raw.resolve()),
            "title": site["name"]
            + (" LROC numeric DTM" if args.elevation else " LROC NAC orthophoto"),
        }
        lat, lon = site["latitudeDeg"], site["longitudeDeg"]
        if args.info:
            print(
                f"{config['title']}\n{site['expectedBytes']:,} bytes\n{site['sourcePage']}\n"
                f"Center: {lat}°, {lon}°; radius {args.radius_meters} m\nOutput: {output}\nBase dataset: {base}"
            )
            continue
        config_path = Path(str(raw) + ".source.json")
        atomic_json(config_path, config)
        execute("fetch", "--config", config_path)
        common = [
            "--source",
            raw,
            "--output",
            output,
            "--latitude",
            lat,
            "--longitude",
            lon,
            "--radius-meters",
            args.radius_meters,
        ]
        if args.elevation:
            level = args.max_level or math.ceil(
                math.log2(2 * MOON_RADIUS / (manifest["tileSize"] * site["nativeResolutionMeters"]))
            )
            execute(
                "height-region",
                *common,
                "--id",
                site["id"],
                "--max-level",
                level,
                "--credit",
                site["credit"],
                "--source-page",
                site["sourcePage"],
            )
        else:
            extra = ["--max-level", args.max_level] if args.max_level else []
            execute("region", *common, "--config", config_path, "--base", base, *extra)


def polar(argv):
    catalog = read_json(catalog_path("south-pole.json"))
    parser = argparse.ArgumentParser(prog="tile-service polar")
    parser.add_argument("--info", action="store_true")
    layers = parser.add_mutually_exclusive_group()
    layers.add_argument("--imagery-only", action="store_true")
    layers.add_argument("--elevation-only", action="store_true")
    parser.add_argument("--radius-meters", type=int, default=catalog["defaultRadiusMeters"])
    for key in [
        "max-level",
        "imagery-max-level",
        "elevation-max-level",
        "overview-imagery-max-level",
        "overview-elevation-max-level",
    ]:
        parser.add_argument("--" + key, type=int)
    parser.add_argument("--imagery-output", type=Path, default=Path("public/data/moon/apollo"))
    parser.add_argument(
        "--elevation-output", type=Path, default=Path("public/data/moon/elevation-tiles")
    )
    parser.add_argument("--base", type=Path, default=Path("public/data/moon/wac/manifest.json"))
    parser.add_argument(
        "--overview-radius-meters", type=float, default=catalog["overview"]["radiusMeters"]
    )
    parser.add_argument(
        "--feather-meters", type=float, default=catalog["overview"]["featherMeters"]
    )
    args = parser.parse_args(argv)
    image = not args.elevation_only
    height = not args.imagery_only
    if not 100 <= args.radius_meters <= 20000:
        raise RuntimeError("Radius must be 100..20000 meters")

    def first(*values):
        return next(value for value in values if value is not None)

    image_level = first(args.imagery_max_level, args.max_level, catalog["defaultImageryMaxLevel"])
    height_level = first(
        args.elevation_max_level, args.max_level, catalog["defaultElevationMaxLevel"]
    )
    if not all(1 <= level <= 16 for level in [image_level, height_level]):
        raise RuntimeError("Tile levels must be 1..16")
    if (
        not math.isfinite(args.feather_meters)
        or not 0 < args.feather_meters <= args.overview_radius_meters * 0.5
    ):
        raise RuntimeError("Invalid feather width")
    detail = {**catalog["center"], "radiusMeters": args.radius_meters}

    def overview(level, explicit, default):
        return {
            "latitudeDeg": -90,
            "longitudeDeg": 0,
            "radiusMeters": args.overview_radius_meters,
            "maxLevel": min(level, explicit if explicit is not None else default),
        }

    image_overview = overview(
        image_level, args.overview_imagery_max_level, catalog["overview"]["imageryMaxLevel"]
    )
    height_overview = overview(
        height_level, args.overview_elevation_max_level, catalog["overview"]["elevationMaxLevel"]
    )
    image_plan = plan_coverage({**detail, "maxLevel": image_level}, image_overview) if image else []
    height_plan = (
        plan_coverage({**detail, "maxLevel": height_level}, height_overview) if height else []
    )
    sources = (catalog["imagery"] if image else []) + ([catalog["elevation"]] if height else [])
    print(
        f"South-pole pack · Shackleton {args.radius_meters / 1000:g} km radius\n"
        f"Sources: {sum(s['expectedBytes'] for s in sources) / 1e9:.2f} GB, plus generated tiles.\n"
        f"Planned address upper bounds: {len(image_plan)} imagery, {len(height_plan)} elevation."
    )
    if args.info:
        for source in sources:
            print(f"{source['title']}: {source['expectedBytes']:,} bytes\n{source['sourceUrl']}")
        return
    if image and image_level <= read_json(args.base)["maxLevel"]:
        raise RuntimeError("Imagery max level must exceed the WAC base level")
    if height:
        base = read_json(args.elevation_output / "manifest.json")
        if base.get("projection") != "normalized-cube" or base.get("format") != "uint16-le":
            raise RuntimeError("Expected a numeric cube height base")
    raw = Path("data/raw/moon/polar")
    raw.mkdir(parents=True, exist_ok=True)
    missing = sum(
        max(
            0,
            s["expectedBytes"]
            - next(
                (
                    p.stat().st_size
                    for p in [raw / (s["id"] + ".tif"), raw / (s["id"] + ".tif.part")]
                    if p.exists()
                ),
                0,
            ),
        )
        for s in sources
    )
    if shutil.disk_usage(raw).free < missing + 512 * 1024**2:
        raise RuntimeError("Insufficient disk space for sources plus a 512 MiB reserve")
    for source in sources:
        path = raw / (source["id"] + ".tif")
        config = Path(str(path) + ".source.json")
        atomic_json(config, {**source, "sourceFile": str(path.resolve())})
        execute("fetch", "--config", config)
    common = [
        "--latitude",
        detail["latitudeDeg"],
        "--longitude",
        detail["longitudeDeg"],
        "--radius-meters",
        args.radius_meters,
    ]
    if image:
        paths = [raw / (s["id"] + ".tif") for s in catalog["imagery"]]
        atomic_json(raw / "south-nac-sources.json", list(map(str, paths)))
        execute(
            "mosaic", "--sources", raw / "south-nac-sources.json", "--output", raw / "south-nac.vrt"
        )
        config = raw / "south-pole-nac-v2.source.json"
        atomic_json(
            config,
            {
                "id": "south-pole-nac-v2",
                "replacesRegion": "shackleton-nac",
                "overview": image_overview,
                "featherMeters": args.feather_meters,
                "name": "South pole and Shackleton crater",
                "product": "NAC_POLE_SOUTH_CM_AVG",
                "nativeResolutionMeters": 1,
                "sourcePage": "https://data.lroc.im-ldi.com/lroc/view_rdr/NAC_POLE_SOUTH_CM_AVG",
                "sourceUrl": catalog["imagery"][0]["sourceUrl"],
                "credit": catalog["imagery"][0]["credit"],
                "components": [
                    {**s, "sha256": read_json(str(p) + ".receipt.json")["sha256"]}
                    for s, p in zip(catalog["imagery"], paths, strict=True)
                ],
            },
        )
        execute(
            "region",
            "--config",
            config,
            "--source",
            raw / "south-nac.vrt",
            "--base",
            args.base,
            "--output",
            args.imagery_output,
            "--max-level",
            image_level,
            *common,
        )
    if height:
        coverage = raw / "south-pole-height-coverage-v2.json"
        atomic_json(coverage, {"overview": height_overview, "featherMeters": args.feather_meters})
        source = catalog["elevation"]
        execute(
            "height-region",
            "--source",
            raw / (source["id"] + ".tif"),
            "--output",
            args.elevation_output,
            "--id",
            "south-pole-lola-v2",
            "--coverage-config",
            coverage,
            "--max-level",
            height_level,
            "--input-scale",
            source["inputScale"],
            "--input-offset",
            source["inputOffset"],
            "--credit",
            source["credit"],
            "--source-page",
            source["sourcePage"],
            *common,
        )


def benchmark(argv):
    parser = argparse.ArgumentParser(prog="tile-service benchmark")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--max-level", type=int, choices=range(8), default=3)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="tile-benchmark-") as directory:
        root = Path(directory)
        source = args.source or root / "analytic.tif"
        if not args.source:
            execute("fixture", "--output", source)
        runs = []
        for jobs in [1, 2, 4]:
            output = root / f"jobs-{jobs}"
            started = time.perf_counter()
            execute(
                "tiles",
                "--source",
                source,
                "--output",
                output,
                "--max-level",
                args.max_level,
                "--jobs",
                jobs,
            )
            seconds = time.perf_counter() - started
            count = read_json(output / "manifest.json")["tileCount"]
            runs.append(
                {
                    "jobs": jobs,
                    "seconds": seconds,
                    "tiles": count,
                    "tilesPerSecond": count / seconds,
                }
            )
            shutil.rmtree(output)
        import json

        print(
            json.dumps(
                {
                    "implementation": "Python / rasterio",
                    "maxLevel": args.max_level,
                    "scope": "Fresh generation; warm filesystem cache",
                    "runs": runs,
                },
                indent=2,
            )
        )
