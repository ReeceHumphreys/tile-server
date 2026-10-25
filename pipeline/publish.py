"""Export a self-contained static release, excluding raw rasters and loose tiles."""

import os
import shutil
import tempfile
from pathlib import Path

from pipeline.runtime import atomic_json, check, read_json, sha256


def publish(args):
    args.validate({"--root", "--output"})
    root = Path(args.get("--root", "public/data")).resolve()
    output = Path(args.get("--output", "published")).resolve()
    if output.exists():
        raise RuntimeError("Release already exists; choose a new --output directory")
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise RuntimeError("Release output must not overlap the build data root")
    assets = {}

    def add(path, expected=None):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise RuntimeError(f"Missing or escaping published asset: {path}")
        relative = path.relative_to(root)
        if relative in assets:
            return
        if expected and sha256(path) != expected:
            raise RuntimeError(f"Published asset checksum mismatch: {path}")
        assets[relative] = path

    def storage(directory, value):
        if value.get("type") != "pmtiles" or value.get("profile") != "normalized-cube-v1":
            raise RuntimeError("Unsupported archive storage descriptor")
        for name in value["archives"].values():
            path = directory / name
            digest = path.stem.rsplit("-", 1)[-1]
            if len(digest) != 64:
                raise RuntimeError(f"Archive needs a content hash: {path}")
            add(path, digest)

    documents = []
    for path in sorted(root.glob("*/*/manifest.json")) + sorted(root.glob("*/*/catalog.json")):
        check()
        document = read_json(path)
        if document.get("projection") == "normalized-cube":
            if not document.get("storage"):
                raise RuntimeError(f"Tileset is not packed; run pack first: {path}")
            add(path)
            storage(path.parent, document["storage"])
            for page in document.get("pages", {}).values():
                add(path.parent / page["path"], page["sha256"])
            documents.append(str(path.relative_to(root)))
        elif "sites" in document:
            add(path)
            for site in document["sites"]:
                if not site.get("storage"):
                    raise RuntimeError(f"Regional site is not packed: {site['id']}")
                storage(path.parent / site["id"], site["storage"])
            documents.append(str(path.relative_to(root)))
    if not documents:
        raise RuntimeError("No packed datasets found")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tile-release-", dir=output.parent))
    try:
        for relative, source in assets.items():
            check()
            target = stage / "data" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".pmtiles" and source.stat().st_mode & 0o004:
                try:
                    os.link(source, target)
                    continue
                except OSError:
                    pass
            shutil.copy2(source, target)
            target.chmod(0o644)
        atomic_json(
            stage / "release.json",
            {"version": 1, "documents": documents, "assetCount": len(assets)},
        )
        (stage / "release.json").chmod(0o644)
        stage.chmod(0o755)
        for directory in stage.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        check()
        if output.exists():
            raise RuntimeError("Release output appeared during publication")
        stage.rename(output)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"Published {len(documents)} catalogs/manifests and {len(assets)} assets to {output}")
