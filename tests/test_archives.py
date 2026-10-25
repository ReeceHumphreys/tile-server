import gzip
import hashlib
import shutil

import pytest
from pmtiles.reader import Reader
from pmtiles.tile import Compression

from pipeline.archives import pack_document
from pipeline.publish import publish
from pipeline.runtime import Args
from tests.conftest import build, load, run, save


def archive_tiles(root, storage, paths):
    for path in paths:
        prefix, level, x, y = path.rsplit("/", 3)
        archive = root / storage["archives"][prefix]
        with archive.open("rb") as source:

            def get(offset, length):
                source.seek(offset)
                return source.read(length)

            reader = Reader(get)
            data = reader.get(int(level), int(x), int(y.split(".")[0]))
            if reader.header()["tile_compression"] == Compression.GZIP:
                data = gzip.decompress(data)
            assert reader.metadata()["profile"] == "normalized-cube-v1"
            yield path, data


def test_pack_exact_imagery_and_reuse(source, tmp_path):
    output = tmp_path / "tiles"
    build(source, output)
    path = output / "manifest.json"
    pack_document(path)
    storage = load(path)["storage"]
    assert len(storage["archives"]) == 6
    paths = [p.relative_to(output).as_posix() for p in output.rglob("*.png")]
    expected = {path: (output / path).read_bytes() for path in paths}
    stamps = [(output / path).stat().st_mtime_ns for path in storage["archives"].values()]
    pack_document(path)
    assert load(path)["storage"] == storage
    assert [(output / path).stat().st_mtime_ns for path in storage["archives"].values()] == stamps
    for tile in paths:
        (output / tile).unlink()
    assert dict(archive_tiles(output, storage, paths)) == expected
    published = path.read_bytes()
    with pytest.raises(RuntimeError, match="Missing tile"):
        pack_document(path)
    assert path.read_bytes() == published


def test_pack_paged_heights_and_corruption(tmp_path):
    raw = bytes([32, 78]) * 32 * 16
    (tmp_path / "source.u16").write_bytes(raw)
    config = tmp_path / "source.json"
    save(
        config,
        {
            "body": "Moon",
            "radiusMeters": 1737400,
            "format": "uint16-le",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "width": 32,
            "height": 16,
            "data": "source.u16",
            "dataSha256": hashlib.sha256(raw).hexdigest(),
            "credit": "test",
        },
    )
    output = tmp_path / "heights"
    run("height-tiles", "--source", config, "--output", output, "--tile-size", 16, "--max-level", 1)
    path = output / "manifest.json"
    manifest = load(path)
    paths = [info["path"] for info in manifest["tiles"].values()]
    expected = {path: (output / path).read_bytes() for path in paths}
    manifest["pages"], manifest["pageLevel"] = {}, 0
    for face in range(6):
        fine = {
            key: info for key, info in manifest["tiles"].items() if key.startswith(f"{face}/1/")
        }
        for key in fine:
            del manifest["tiles"][key]
        name, prefix = f"page-{face}.json", f"{face}/0/0/0"
        save(output / name, {"tiles": fine})
        manifest["pages"][prefix] = {
            "path": name,
            "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest(),
            "lower": manifest["tiles"][prefix]["lower"],
            "upper": manifest["tiles"][prefix]["upper"],
        }
    save(path, manifest)
    pack_document(path)
    storage = load(path)["storage"]
    assert dict(archive_tiles(output, storage, paths)) == expected
    snapshot = path.read_bytes()
    (output / paths[0]).write_bytes(b"bad")
    with pytest.raises(RuntimeError, match="checksum"):
        pack_document(path)
    assert path.read_bytes() == snapshot
    page = output / "page-0.json"
    page.write_text("{}")
    with pytest.raises(RuntimeError, match="metadata checksum"):
        pack_document(path)


def test_regional_archive_and_static_release(source, tmp_path):
    root, release = tmp_path / "build", tmp_path / "release"
    output = root / "moon/imagery"
    build(source, output)
    pack_document(output / "manifest.json")
    site = root / "moon/regions/site-one"
    site.mkdir(parents=True)
    shutil.copytree(output / "0", site / "0")
    catalog = root / "moon/regions/catalog.json"
    save(catalog, {"sites": [{"id": "site-one", "tiles": ["0/1/0/0"]}]})
    pack_document(catalog)
    storage = load(catalog)["sites"][0]["storage"]
    assert (
        dict(archive_tiles(site, storage, ["0/1/0/0.png"]))["0/1/0/0.png"]
        == (site / "0/1/0/0.png").read_bytes()
    )
    # Private-mode legacy archives must be copied with readable deployment
    # permissions without changing the build files' permissions.
    private_archive = output / next(
        iter(load(output / "manifest.json")["storage"]["archives"].values())
    )
    private_archive.chmod(0o600)
    # Raw inputs, loose tiles and receipts are never part of a serving release.
    (root / "private.txt").write_text("private")
    publish(Args(["tile-service", "publish", "--root", str(root), "--output", str(release)]))
    assets = list(release.rglob("*"))
    assert len(list(release.rglob("*.pmtiles"))) == 7
    assert not any(p.suffix in (".png", ".u16", ".tif") or p.name == "private.txt" for p in assets)
    assert not list(release.rglob("*.part"))
    assert load(release / "release.json")["assetCount"] == 9
    assert release.stat().st_mode & 0o005 == 0o005
    assert all(path.stat().st_mode & 0o004 for path in assets if path.is_file())
    assert private_archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        publish(Args(["tile-service", "publish", "--root", str(root), "--output", str(release)]))
    (root / "moon/imagery/manifest.json").write_text("changed")
    assert load(release / "data/moon/imagery/manifest.json")["storage"]


def test_release_rejects_unpacked_and_escaping_data(source, tmp_path):
    root, output = tmp_path / "build", tmp_path / "release"
    dataset = root / "moon/imagery"
    build(source, dataset)
    args = Args(["tile-service", "publish", "--root", str(root), "--output", str(output)])
    with pytest.raises(RuntimeError, match="not packed"):
        publish(args)
    assert not output.exists()
    pack_document(dataset / "manifest.json")
    manifest = load(dataset / "manifest.json")
    archive = dataset / manifest["storage"]["archives"]["0"]
    escaped = tmp_path / archive.name
    archive.rename(escaped)
    archive.symlink_to(escaped)
    with pytest.raises(RuntimeError, match="escaping"):
        publish(args)
    assert not output.exists()


def test_installed_cli_help_and_unknown_commands():
    assert "pack" in run("--help") and "publish" in run("--help")
    assert "invalid choice" in run("unknown-command", ok=False)
    assert "--max-level" in run("tiles", "--help")
