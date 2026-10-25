"""Publish existing cube tiles as immutable PMTiles v3 archives.

Coordinates remain face-local, bottom-left Y-up: these are not Mercator maps.
Only the final atomic manifest/catalog replacement switches readers to archives.
"""

import gzip
import hashlib
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path

from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from pipeline.runtime import atomic_json, check, log, read_json, sha256

ADDRESS = re.compile(r"^((?:[a-zA-Z0-9_-]+/)*)([0-5])/(\d+)/(\d+)/(\d+)\.(png|u16)$")


def _entries(document, root):
    if document.get("projection") != "normalized-cube":
        raise RuntimeError("PMTiles packaging requires a normalized-cube manifest")
    if document.get("format") == "png":
        if document.get("addressing", {}).get("template") != "{face}/{level}/{x}/{y}.png":
            raise RuntimeError("Unsupported imagery address template")
        for f in range(6):
            for z in range(document["maxLevel"] + 1):
                for x in range(1 << z):
                    for y in range(1 << z):
                        yield f"{f}/{z}/{x}/{y}.png", None
    elif document.get("format") == "uint16-le":
        tiles = dict(document["tiles"])
        for page in document.get("pages", {}).values():
            path = _local(root, page["path"])
            if sha256(path) != page["sha256"]:
                raise RuntimeError(f"Height metadata checksum mismatch: {path}")
            for key, info in read_json(path)["tiles"].items():
                if key in tiles:
                    raise RuntimeError(f"Duplicate height address: {key}")
                tiles[key] = info
        for key, info in tiles.items():
            if not info["path"].endswith(f"{key}.u16"):
                raise RuntimeError(f"Height address/path mismatch: {key}")
            yield info["path"], info["sha256"]
    else:
        raise RuntimeError("Only PNG imagery and uint16-le heights can be packed")


def _local(root, path):
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise RuntimeError(f"Path escapes dataset: {path}")
    return candidate


def _storage(root, entries, metadata):
    groups = defaultdict(list)
    for path, expected_hash in entries:
        check()
        match = ADDRESS.fullmatch(path)
        if not match:
            raise RuntimeError(f"Invalid cube tile path: {path}")
        prefix, face, z, x, y, extension = match.groups()
        z, x, y = int(z), int(x), int(y)
        if z > 20 or x >= 1 << z or y >= 1 << z:
            raise RuntimeError(f"Invalid cube tile coordinates: {path}")
        source = _local(root, path)
        if not source.is_file():
            raise RuntimeError(f"Missing tile: {source}")
        groups[prefix + face].append((zxy_to_tileid(z, x, y), source, expected_hash, extension))
    if not groups:
        raise RuntimeError("Cannot publish an empty tileset")
    out = root / "pmtiles"
    out.mkdir(exist_ok=True)
    archives = {}
    for key, records in sorted(groups.items()):
        check()
        records.sort(key=lambda record: record[0])
        if len({r[0] for r in records}) != len(records):
            raise RuntimeError(f"Duplicate archive coordinates: {key}")
        extensions = {r[3] for r in records}
        if len(extensions) != 1:
            raise RuntimeError(f"Mixed payload formats: {key}")
        numeric = records[0][3] == "u16"
        # Fingerprint content, not mtimes. Repeated packaging reuses an existing
        # archive; changed source pixels produce a new immutable URL.
        identity = hashlib.sha256(repr(sorted(metadata.items())).encode())
        verified = []
        for tile_id, source, expected, extension in records:
            check()
            digest = sha256(source)
            if expected and expected != digest:
                raise RuntimeError(f"Height checksum mismatch: {source}")
            identity.update(f"{tile_id}:{digest}\n".encode())
            verified.append((tile_id, source, digest, extension))
        records = verified
        stem = key.replace("/", "-")
        receipt = out / f"{stem}-{identity.hexdigest()}.json"
        if receipt.exists():
            previous = read_json(receipt)
            archive = _local(root, previous["path"])
            if archive.is_file() and sha256(archive) == previous["sha256"]:
                archives[key] = previous["path"]
                log(f"Reusing {archive} ({len(records)} tiles)")
                continue
        descriptor, temp_name = tempfile.mkstemp(prefix=stem + "-", suffix=".part", dir=out)
        temp = Path(temp_name)
        writer = None
        try:
            with os.fdopen(descriptor, "wb") as target:
                writer = Writer(target)
                for tile_id, source, expected, _ in records:
                    check()
                    data = source.read_bytes()
                    if hashlib.sha256(data).hexdigest() != expected:
                        raise RuntimeError(f"Tile changed during packaging: {source}")
                    writer.write_tile(tile_id, gzip.compress(data, mtime=0) if numeric else data)
                writer.finalize(
                    {
                        "tile_type": TileType.UNKNOWN if numeric else TileType.PNG,
                        "tile_compression": Compression.GZIP if numeric else Compression.NONE,
                    },
                    {
                        **metadata,
                        "profile": "normalized-cube-v1",
                        "archiveKey": key,
                        "yDirection": "up",
                        "format": "uint16-le" if numeric else "png",
                    },
                )
            check()
            # Confirm this is a readable archive before making it discoverable.
            with temp.open("rb") as source:

                def read(offset, length):
                    source.seek(offset)
                    return source.read(length)

                reader = Reader(read)
                if reader.header()["addressed_tiles_count"] != len(records):
                    raise RuntimeError("Archive tile count mismatch")
                if reader.metadata()["profile"] != "normalized-cube-v1":
                    raise RuntimeError("Archive metadata mismatch")
            digest = sha256(temp)
            destination = out / f"{stem}-{digest}.pmtiles"
            temp.chmod(0o644)
            os.replace(temp, destination)
            relative = destination.relative_to(root).as_posix()
            atomic_json(receipt, {"path": relative, "sha256": digest})
            archives[key] = relative
            log(f"Packed {destination} ({len(records)} tiles)")
        finally:
            if writer:
                writer.tile_f.close()
            temp.unlink(missing_ok=True)
    return {"type": "pmtiles", "profile": "normalized-cube-v1", "archives": archives}


def pack_document(path):
    path = Path(path).resolve()
    root = path.parent
    document = read_json(path)
    metadata = {k: document[k] for k in ("body", "radiusMeters", "id", "format") if k in document}
    if "sites" in document:
        # Each regional site is an independently replaceable group of face archives.
        for site in document["sites"]:
            check()
            site_root = _local(root, site["id"])
            site["storage"] = _storage(
                site_root,
                ((f"{address}.png", None) for address in site["tiles"]),
                {**metadata, "id": site["id"]},
            )
    else:
        document["storage"] = _storage(root, _entries(document, root), metadata)
    check()
    atomic_json(path, document)
    log(f"Published PMTiles references in {path}")


def pack(args):
    args.validate({"--manifest", "--all", "--root"})
    if args.has("--all"):
        if args.has("--manifest"):
            raise RuntimeError("Choose --all or --manifest")
        root = Path(args.get("--root", "public/data"))
        paths = sorted(root.glob("*/*/manifest.json")) + sorted(root.glob("*/*/catalog.json"))
        selected = [
            p
            for p in paths
            if read_json(p).get("projection") == "normalized-cube" or "sites" in read_json(p)
        ]
        if not selected:
            raise RuntimeError("No cube tilesets found")
        for path in selected:
            pack_document(path)
    elif args.has("--manifest"):
        pack_document(args.get("--manifest"))
    else:
        raise RuntimeError("Use --manifest path/to/manifest.json (or catalog.json), or --all")
