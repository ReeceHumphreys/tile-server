"""Build planetary datasets from datasets/catalog.json, then release them as PMTiles.

tile-service list
tile-service build moon/wac
tile-service build moon/apollo --site apollo12
tile-service release --output releases/v2
tile-service upload releases/v2 --bucket planet-tiles --release v2
"""

import argparse
import sys
from importlib.metadata import version
from pathlib import Path


def _workspace(args):
    from tile_service import Workspace

    return Workspace(args.workspace)


def _list(args):
    ws = _workspace(args)
    for name, spec in ws.targets().items():
        state = ws.status(name)
        print(f"{name:28} {state:6} {spec.get('description', spec['layer'])}")


def _build(args):
    _workspace(args).build(
        *args.targets,
        output=args.output,
        source=args.source,
        max_level=args.max_level,
        radius_meters=args.radius_meters,
        sites=tuple(args.site or ()),
        jobs=args.jobs,
        dry_run=args.dry_run,
    )


def _release(args):
    _workspace(args).release(args.output, pack=not args.no_pack)


def _upload(args):
    from tile_service import s3

    if args.local:
        s3.init_local(args.bucket, args.endpoint_url)
    _workspace(args).upload(
        args.release_dir,
        bucket=args.bucket,
        release_id=args.release,
        endpoint=args.endpoint_url,
        part_size_mib=args.part_size_mib,
    )


WORKSPACE = ("-w", "--workspace")
WORKSPACE_OPTIONS = {
    "default": ".",
    "help": "project directory holding datasets/, data/raw/ and public/data/ (default .)",
}


def parser():
    top = argparse.ArgumentParser(
        prog="tile-service", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    top.add_argument("--version", action="version", version=version("tile-service"))
    commands = top.add_subparsers(dest="command", required=True, metavar="COMMAND")

    listing = commands.add_parser("list", help="show build targets and whether they exist")
    listing.add_argument(*WORKSPACE, **WORKSPACE_OPTIONS)
    listing.set_defaults(run=_list)

    build = commands.add_parser(
        "build",
        help="download sources and build targets (resumable)",
        description="Download, verify and build catalog targets. Rerunning resumes or verifies.",
    )
    build.add_argument("targets", nargs="+", metavar="TARGET", help="target name(s), or all")
    build.add_argument(*WORKSPACE, **WORKSPACE_OPTIONS)
    build.add_argument("--output", type=Path, help="write somewhere other than BUILDS/TARGET")
    build.add_argument("--source", type=Path, help="local raster instead of the catalog download")
    build.add_argument("--max-level", type=int, help="override the deepest tile level")
    build.add_argument("--radius-meters", type=float, help="override a regional disk radius")
    build.add_argument("--site", action="append", help="only this regional site (repeatable)")
    build.add_argument("--jobs", type=int, default=2, help="face workers for imagery (1..6)")
    build.add_argument("--dry-run", action="store_true", help="describe sources and outputs only")
    build.set_defaults(run=_build)

    release = commands.add_parser(
        "release",
        help="pack PMTiles and export a static release",
        description="Pack every built tileset into PMTiles, then copy packed documents and "
        "archives into a new release directory. Raw rasters and loose tiles are excluded.",
    )
    release.add_argument(*WORKSPACE, **WORKSPACE_OPTIONS)
    release.add_argument("--output", default="published", help="new release directory")
    release.add_argument("--no-pack", action="store_true", help="export already packed data only")
    release.set_defaults(run=_release)

    upload = commands.add_parser(
        "upload",
        help="upload a release to S3 under releases/ID/",
        description="Upload an exported release to S3, resumably. TILE_BUCKET, TILE_RELEASE "
        "and AWS_ENDPOINT_URL_S3 supply defaults.",
    )
    upload.add_argument(*WORKSPACE, **WORKSPACE_OPTIONS)
    upload.add_argument("release_dir", metavar="RELEASE_DIR", help="exported release directory")
    upload.add_argument("--bucket", help="bucket (default $TILE_BUCKET)")
    upload.add_argument("--release", help="release ID (default $TILE_RELEASE)")
    upload.add_argument("--endpoint-url", help="S3 endpoint (default $AWS_ENDPOINT_URL_S3)")
    upload.add_argument("--part-size-mib", type=int, default=64, help="multipart size, 5..512")
    upload.add_argument(
        "--local", action="store_true", help="first create the bucket on a local emulator"
    )
    upload.set_defaults(run=_upload)
    return top


def main(argv=None):
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        from tile_service.runtime import install_signals

        install_signals()
        args.run(args)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"tile-service: {exc}", file=sys.stderr)
        return 1
