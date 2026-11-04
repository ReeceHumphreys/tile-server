"""Releases: pack cube tiles into PMTiles, then export a self-contained static release.

PMTiles coordinates remain face-local, bottom-left Y-up: these are not Mercator
maps. Only the final atomic manifest/catalog replacement switches readers to
archives. A release holds packed documents, their archives and height pages;
never raw rasters or loose tiles.
"""

import gzip
import hashlib
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from pmtiles.reader import Reader
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from tile_service import documents
from tile_service.documents import PROFILE
from tile_service.height_index import HeightIndex
from tile_service.runtime import atomic_json, check, log, read_json, sha256
from tile_service.tile_store import ADDRESS


def published(root):
    """(path, document) for every published document under `root`."""
    root = Path(root)
    for path in sorted(root.rglob("manifest.json")) + sorted(root.rglob("catalog.json")):
        check()
        document = read_json(path)
        if documents.kind(document):
            yield path, document


def validate_all(found):
    """Validate (path, document) pairs as the globe would; catalogs against their base."""
    imagery = [d for _, d in found if documents.kind(d) == "imagery"]
    for path, document in found:
        base = None
        if documents.kind(document) == "regions":
            base = next(
                (
                    candidate
                    for candidate in imagery
                    if candidate.get("id") == document.get("baseId")
                    and candidate.get("maxLevel") == document.get("baseMaxLevel")
                ),
                None,
            )
            if base is None:
                raise RuntimeError(f"Regional catalog's base imagery is not in the release: {path}")
        try:
            documents.validate(document, base)
        except documents.InvalidDocument as error:
            raise RuntimeError(f"The globe would reject {path}: {error}") from None


def _entries(document, root):
    if documents.kind(document) not in ("imagery", "heights"):
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
        for key, info in HeightIndex(root, document).all_tiles().items():
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
                        "profile": PROFILE,
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
                if reader.metadata()["profile"] != PROFILE:
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
    return {"type": "pmtiles", "profile": PROFILE, "archives": archives}


def _intact(root, storage):
    """Archives a document already references, all present on disk.

    Builds drop `storage` whenever they change tiles, so present archives are
    current; their content hashes are verified again at export. Keeping them
    means loose tiles may be deleted after packing.
    """
    if not storage or storage.get("type") != "pmtiles" or storage.get("profile") != PROFILE:
        return False
    return all(_local(root, name).is_file() for name in storage["archives"].values())


def pack_document(path):
    """Pack one manifest or regional catalog; already packed parts are kept."""
    path = Path(path).resolve()
    root = path.parent
    document = read_json(path)
    metadata = {k: document[k] for k in ("body", "radiusMeters", "id", "format") if k in document}
    changed = False
    if "sites" in document:
        # Each regional site is an independently replaceable group of face archives.
        for site in document["sites"]:
            check()
            site_root = _local(root, site["id"])
            if _intact(site_root, site.get("storage")):
                continue
            site["storage"] = _storage(
                site_root,
                ((f"{address}.png", None) for address in site["tiles"]),
                {**metadata, "id": site["id"]},
            )
            changed = True
    elif not _intact(root, document.get("storage")):
        document["storage"] = _storage(root, _entries(document, root), metadata)
        changed = True
    check()
    if changed:
        atomic_json(path, document)
        log(f"Published PMTiles references in {path}")
    else:
        log(f"Already packed: {path}")


def pack(root):
    """Pack every tileset and regional catalog under `root`."""
    selected = [path for path, _ in published(root)]
    if not selected:
        raise RuntimeError(f"No cube tilesets found under {root}")
    for path in selected:
        pack_document(path)
    return selected


def export(root, output):
    """Copy packed documents and their assets from `root` into a new release directory."""
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise RuntimeError("Release already exists; choose a new output directory")
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise RuntimeError("Release output must not overlap the build data root")
    files = {}

    def add(path, expected=None):
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise RuntimeError(f"Missing or escaping published asset: {path}")
        relative = path.relative_to(root)
        if relative in files:
            return
        if expected and sha256(path) != expected:
            raise RuntimeError(f"Published asset checksum mismatch: {path}")
        files[relative] = path

    found_documents = list(published(root))
    validate_all(found_documents)
    names = []
    for path, document in found_documents:
        found = documents.assets(document)
        add(path)
        for relative, expected in found:
            add(path.parent / relative, expected)
        names.append(path.relative_to(root).as_posix())
    if not names:
        raise RuntimeError("No packed datasets found")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".tile-release-", dir=output.parent))
    try:
        for relative, source in files.items():
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
            {"version": 1, "documents": names, "assetCount": len(files)},
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
    log(f"Published {len(names)} catalogs/manifests and {len(files)} assets to {output}")
    return output


def files(release):
    """Every file of an exported release, verified: {relative name: (path, sha256)}."""
    release = Path(release).resolve()
    listing = read_json(release / "release.json")
    found = {}

    def add(relative, expected=None):
        path = release / relative
        if not path.resolve().is_relative_to(release) or not path.is_file():
            raise RuntimeError(f"Missing or escaping release asset: {relative}")
        digest = sha256(path)
        if expected and digest != expected:
            raise RuntimeError(f"Release asset checksum mismatch: {relative}")
        found[relative.as_posix()] = (path, digest)

    for name in listing["documents"]:
        relative = Path("data") / name
        add(relative)
        for asset, expected in documents.assets(read_json(release / relative)):
            add(relative.parent / asset, expected)
    if len(found) != listing["assetCount"]:
        raise RuntimeError("Release asset count mismatch")
    add(Path("release.json"))
    return found
