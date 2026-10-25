"""Command-line entry point for offline dataset builds and publication."""

import argparse
import importlib
import sys
from importlib.metadata import version

COMMANDS = {
    "init-s3": (
        "s3",
        "init_s3",
        "Initialize a local S3 emulator bucket and CORS (--bucket, --endpoint-url).",
    ),
    "publish-s3": (
        "s3",
        "publish_s3",
        "Upload a static release (--source published, --bucket, --release, --endpoint-url).",
    ),
    "seed-s3": (
        "s3",
        "seed_s3",
        "Initialize and populate the local emulator (--source published).",
    ),
    "fetch": ("sources", "fetch", "Download and verify a source raster (--config, --info)."),
    "tiles": (
        "tiles",
        "tiles",
        "Generate imagery (--config, --source, --output, --max-level, --jobs).",
    ),
    "cloudless": (
        "cloudless",
        "cloudless",
        "Prepare EOxCloudless imagery (--zoom, --output, --info).",
    ),
    "mosaic": ("sources", "mosaic", "Build a VRT (--sources list.json, --output mosaic.vrt)."),
    "region": (
        "region",
        "region",
        "Generate regional imagery (--config, --source, --base, --output, --latitude, --longitude, --radius-meters, --max-level).",
    ),
    "earth-sites": ("sites", "earth_sites", "Prepare launch-site imagery (--site, --info)."),
    "height-tiles": (
        "heights",
        "height_tiles",
        "Generate elevation tiles (--source, --output, --tile-size, --max-level).",
    ),
    "height-region": (
        "heights",
        "height_region",
        "Add regional elevation (--source, --output, --id, --latitude, --longitude, --radius-meters, --max-level).",
    ),
    "prepare-elevation": (
        "sources",
        "prepare_elevation",
        "Prepare a numeric elevation grid (--source, --output, --body, --body-radius, --width, --height).",
    ),
    "elevation": (
        "sources",
        "elevation",
        "Prepare global lunar LOLA elevation (--output, --info).",
    ),
    "earth-elevation": (
        "sources",
        "earth_elevation",
        "Prepare ETOPO elevation (--source, --output, --info).",
    ),
    "fixture": (
        "sources",
        "fixture",
        "Create an analytic raster (--output, --height, --constant).",
    ),
    "inspect": ("sources", "inspect", "Extract raster pixels (--source, --output)."),
    "pack": (
        "archives",
        "pack",
        "Package PMTiles (--manifest path, or --all [--root public/data]).",
    ),
    "publish": (
        "publish",
        "publish",
        "Export only published assets (--root public/data, --output published).",
    ),
}
JOBS = {
    "apollo": "Prepare Apollo imagery or elevation.",
    "polar": "Prepare polar coverage.",
    "benchmark": "Measure fresh imagery builds with 1, 2, and 4 workers.",
}


def dispatch(args):
    module, function, _ = COMMANDS[args.command]
    return getattr(importlib.import_module("pipeline." + module), function)(args)


def main(argv=None):
    argv = sys.argv if argv is None else argv
    parser = argparse.ArgumentParser(prog="tile-service", description=__doc__)
    parser.add_argument("--version", action="version", version=version("tile-service"))
    parser.add_argument("command", nargs="?", choices=[*COMMANDS, *JOBS], help="offline operation")
    parser.epilog = "\n".join(f"{key}: {value[2]}" for key, value in COMMANDS.items())
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    if len(argv) < 2 or argv[1] in ("--help", "-h", "--version"):
        parser.parse_args(argv[1:])
        if len(argv) < 2:
            parser.print_help()
        return 0
    parser.parse_args(argv[1:2])
    try:
        from pipeline.runtime import Args, install_signals

        install_signals()
        if argv[1] in JOBS:
            from pipeline import jobs

            getattr(jobs, argv[1])(argv[2:])
        elif "--help" in argv[2:] or "-h" in argv[2:]:
            print(COMMANDS[argv[1]][2])
            print("See docs/data.md and docs/tile-generation.md for all settings.")
        else:
            dispatch(Args(argv))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"tile-service: {exc}", file=sys.stderr)
        return 1
