import gzip
import hashlib

import pytest
from pmtiles.reader import Reader
from pmtiles.tile import Compression

from tests.conftest import WAC, build, fixture, load, run, save
from tile_service import documents, heights, regions
from tile_service.releases import export, pack, pack_document
from tile_service.tile_store import TileStore

SITE = {
    "id": "site",
    "name": "Site",
    "credit": "Synthetic",
    "sourcePage": "https://example.test/site",
    "nativeResolutionMeters": 0.5,
}


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
    # Packed datasets no longer need loose tiles: packing and rebuilding keep the archives.
    published = path.read_bytes()
    pack_document(path)
    assert path.read_bytes() == published
    assert build(source, output)["storage"] == storage
    store = TileStore.for_document(path)
    assert all(store.read(tile) == data for tile, data in expected.items())
    # Without archive references, packing needs the loose tiles again.
    save(path, {key: value for key, value in load(path).items() if key != "storage"})
    with pytest.raises(RuntimeError, match="Missing tile"):
        pack_document(path)


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
    heights.build(config, output, tile_size=16, max_level=1)
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
    store = TileStore.for_document(path)
    for tile in paths:
        (output / tile).unlink()
    assert all(store.read(tile) == data for tile, data in expected.items())
    for tile, data in expected.items():
        (output / tile).write_bytes(data)
    # A rebuild drops archive references; packing then verifies every tile again.
    save(path, {key: value for key, value in load(path).items() if key != "storage"})
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
    nac = fixture(tmp_path / "nac.tif", constant=200)
    site = regions.build(
        nac,
        output / "manifest.json",
        root / "moon/regions",
        {**SITE, "id": "site-one"},
        0,
        0,
        1000,
        3,
    )
    catalog = root / "moon/regions/catalog.json"
    pack_document(catalog)
    storage = load(catalog)["sites"][0]["storage"]
    tile = site["tiles"][-1] + ".png"
    assert (
        dict(archive_tiles(root / "moon/regions/site-one", storage, [tile]))[tile]
        == (root / "moon/regions/site-one" / tile).read_bytes()
    )
    # Private-mode legacy archives must be copied with readable deployment
    # permissions without changing the build files' permissions.
    private_archive = output / next(
        iter(load(output / "manifest.json")["storage"]["archives"].values())
    )
    private_archive.chmod(0o600)
    # Raw inputs, loose tiles and receipts are never part of a serving release.
    (root / "private.txt").write_text("private")
    export(root, release)
    assets = list(release.rglob("*"))
    assert len(list(release.rglob("*.pmtiles"))) == 7
    assert not any(p.suffix in (".png", ".u16", ".tif") or p.name == "private.txt" for p in assets)
    assert not list(release.rglob("*.part"))
    assert load(release / "release.json")["assetCount"] == 9
    assert release.stat().st_mode & 0o005 == 0o005
    assert all(path.stat().st_mode & 0o004 for path in assets if path.is_file())
    assert private_archive.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        export(root, release)
    (root / "moon/imagery/manifest.json").write_text("changed")
    assert load(release / "data/moon/imagery/manifest.json")["storage"]


def test_release_rejects_unpacked_and_escaping_data(source, tmp_path):
    root, output = tmp_path / "build", tmp_path / "release"
    dataset = root / "moon/imagery"
    build(source, dataset)
    with pytest.raises(RuntimeError, match="not packed"):
        export(root, output)
    assert not output.exists()
    assert pack(root) == [dataset / "manifest.json"]
    manifest = load(dataset / "manifest.json")
    archive = dataset / manifest["storage"]["archives"]["0"]
    escaped = tmp_path / archive.name
    archive.rename(escaped)
    archive.symlink_to(escaped)
    with pytest.raises(RuntimeError, match="escaping"):
        export(root, output)
    assert not output.exists()


def test_installed_cli_help_and_unknown_commands(tmp_path):
    usage = run("--help")
    assert all(command in usage for command in ("list", "build", "release", "upload"))
    assert "invalid choice" in run("unknown-command", ok=False)
    assert "--max-level" in run("build", "--help")
    assert "moon/wac" in run("list", "--workspace", tmp_path)
    assert "No cube tilesets" in run("release", "--workspace", tmp_path, ok=False)


def test_provenance_edits_republish_without_rebuilding(source, tmp_path):
    base, regional = tmp_path / "base", tmp_path / "regional"
    build(source, base)
    pack_document(base / "manifest.json")
    nac = fixture(tmp_path / "nac.tif", constant=200)
    site = {
        "id": "site",
        "name": "Site",
        "credit": "Old credit",
        "sourcePage": "https://example.test/site",
        "nativeResolutionMeters": 0.5,
    }
    regions.build(nac, base / "manifest.json", regional, site, 0, 0, 1000, 4)
    pack_document(regional / "catalog.json")
    tile = base / "0/0/0/0.png"
    stamps = (tile.stat().st_mtime_ns, (regional / "site/0/4/8/8.png").stat().st_mtime_ns)

    retitled = {**WAC, "title": "Renamed mosaic", "credit": "New credit"}
    manifest = build(source, base, retitled)
    assert manifest["title"] == "Renamed mosaic" and manifest["source"]["credit"] == "New credit"
    assert manifest["storage"] == load(base / "manifest.json")["storage"]
    entry = regions.build(
        nac, base / "manifest.json", regional, {**site, "credit": "New credit"}, 0, 0, 1000, 4
    )
    assert entry["source"]["credit"] == "New credit" and entry["storage"]
    assert load(regional / "catalog.json")["sites"] == [entry]
    assert (tile.stat().st_mtime_ns, (regional / "site/0/4/8/8.png").stat().st_mtime_ns) == stamps
    with pytest.raises(RuntimeError, match="another source/settings"):
        regions.build(
            nac, base / "manifest.json", regional, {**site, "featherMeters": 50}, 0, 0, 1000, 4
        )


def test_older_build_records_still_resume(source, tmp_path):
    base, regional = tmp_path / "base", tmp_path / "regional"
    build(source, base)
    state = load(base / "build-state.json")
    old = {**state, "dataset": WAC, "strategy": "cached"}
    del old["radiusMeters"]
    save(base / "build-state.json", old)
    build(source, base)
    nac = fixture(tmp_path / "nac.tif", constant=200)
    site = SITE
    regions.build(nac, base / "manifest.json", regional, site, 0, 0, 1000, 4)
    record = load(regional / "site/build-state.json")
    for key in ("overview", "featherMeters", "base"):
        del record[key]
    record["baseManifestSha256"] = regions._manifest_hash(load(base / "manifest.json"))
    record["sourceConfig"] = {**site, "sourceFile": "/elsewhere/nac.tif", "note": "edited"}
    save(regional / "site/build-state.json", record)
    regions.build(nac, base / "manifest.json", regional, site, 0, 0, 1000, 4)


def test_regions_build_on_packed_bases_without_loose_tiles(source, tmp_path):
    def tree(root):
        return {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*")
            if p.suffix in (".png", ".u16")
        }

    nac = fixture(tmp_path / "nac.tif", constant=200)
    dem = fixture(tmp_path / "dem.tif", height=True, constant=40)
    raw = bytes([32, 78]) * 32 * 16
    (tmp_path / "grid.u16").write_bytes(raw)
    save(
        tmp_path / "grid.json",
        {
            "body": "Moon",
            "radiusMeters": 1737400,
            "format": "uint16-le",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "width": 32,
            "height": 16,
            "data": "grid.u16",
            "dataSha256": hashlib.sha256(raw).hexdigest(),
            "credit": "test",
        },
    )
    site = SITE
    outputs = {}
    for mode in ("loose", "packed"):
        base, hts = tmp_path / mode / "base", tmp_path / mode / "heights"
        build(source, base)
        heights.build(tmp_path / "grid.json", hts, tile_size=16, max_level=1)
        if mode == "packed":
            pack_document(base / "manifest.json")
            pack_document(hts / "manifest.json")
            for loose in [*base.rglob("*.png"), *hts.rglob("*.u16")]:
                loose.unlink()
        regions.build(
            nac, base / "manifest.json", tmp_path / mode / "regional", site, 0, 0, 1000, 4
        )
        heights.add_region(hts, dem, "patch", 0, 0, 500, max_level=3)
        outputs[mode] = (tree(tmp_path / mode / "regional"), tree(hts / "patch"))
    assert outputs["loose"] == outputs["packed"] and outputs["packed"][0] and outputs["packed"][1]
    # The region retired the archives; packing again still has every tile.
    packed = tmp_path / "packed/heights/manifest.json"
    pack_document(packed)
    store = TileStore.for_document(packed)
    tiles = heights.HeightIndex.open(packed.parent).all_tiles().values()
    assert all(store.read(tile["path"]) for tile in tiles)


def test_retiring_archives_restores_deleted_tiles(source, tmp_path):
    output = tmp_path / "base"
    build(source, output)
    expected = {p.relative_to(output).as_posix(): p.read_bytes() for p in output.rglob("*.png")}
    pack_document(output / "manifest.json")
    for tile in expected:
        (output / tile).unlink()
    assert "storage" not in build(source, output, {**WAC, "id": "renamed"})
    pack_document(output / "manifest.json")
    store = TileStore.for_document(output / "manifest.json")
    assert all(store.read(tile) == data for tile, data in expected.items())


def test_documents_follow_the_globe_contract(source, tmp_path):
    base = build(source, tmp_path / "build/moon/base")
    nac = fixture(tmp_path / "nac.tif", constant=200)
    regions.build(
        nac,
        tmp_path / "build/moon/base/manifest.json",
        tmp_path / "build/moon/regions",
        SITE,
        0,
        0,
        1000,
        4,
    )
    catalog = load(tmp_path / "build/moon/regions/catalog.json")
    raw = bytes([32, 78]) * 32 * 16
    (tmp_path / "grid.u16").write_bytes(raw)
    save(
        tmp_path / "grid.json",
        {
            "body": "Moon",
            "radiusMeters": 1737400,
            "format": "uint16-le",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "width": 32,
            "height": 16,
            "data": "grid.u16",
            "dataSha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    height = heights.build(
        tmp_path / "grid.json", tmp_path / "build/moon/heights", tile_size=16, max_level=1
    )
    for document in (base, height):
        documents.validate(document)
    documents.validate(catalog, base)
    assert [documents.kind(d) for d in (base, height, catalog)] == ["imagery", "heights", "regions"]

    site = catalog["sites"][0]
    child = next(key for key in site["tiles"] if key.split("/")[1] == "4")
    parentless = {**site, "tiles": [key for key in site["tiles"] if key.split("/")[1] != "3"]}
    root = next(iter(height["tiles"]))
    rejected = [
        ({**base, "textureUV": {"offset": 0.1, "scale": 0.8}}, None, "coordinate convention"),
        ({**base, "storage": {"type": "pmtiles", "profile": "x", "archives": {}}}, None, "storage"),
        ({**catalog, "baseMaxLevel": 0}, base, "Incompatible"),
        ({**catalog, "sites": [{**site, "source": {}}]}, base, "Invalid regional imagery site"),
        ({**catalog, "sites": [site, {**site, "id": "copy"}]}, base, "overlapping"),
        ({**catalog, "sites": [parentless]}, base, "missing its parent"),
        (
            {
                **height,
                "tiles": {**height["tiles"], root: {**height["tiles"][root], "upper": -99999}},
            },
            None,
            "",
        ),
        ({**height, "minMeters": -20000}, None, "Unsupported height"),
    ]
    assert child
    for document, against, message in rejected:
        with pytest.raises(documents.InvalidDocument, match=message):
            documents.validate(document, against)

    pack_document(tmp_path / "build/moon/base/manifest.json")
    save(
        tmp_path / "build/moon/base/manifest.json",
        {**load(tmp_path / "build/moon/base/manifest.json"), "minLevel": 1},
    )
    with pytest.raises(RuntimeError, match="globe would reject"):
        export(tmp_path / "build", tmp_path / "release")
    assert not (tmp_path / "release").exists()


def test_legacy_pyramid_stays_accepted_without_loose_tiles(source, tmp_path):
    output = tmp_path / "base"
    build(source, output)
    pack_document(output / "manifest.json")
    for tile in output.rglob("*.png"):
        tile.unlink()
    state = load(output / "build-state.json")
    save(
        output / "build-state.json",
        {**state, "pipelineVersion": 2, "dataset": {"radiusMeters": state["radiusMeters"]}},
    )
    assert "storage" in build(source, output)


def test_release_finds_targets_at_any_depth(source, tmp_path):
    root = tmp_path / "data"
    for name in ("moon/base", "mars/hirise/site", "pluto"):
        build(source, root / name)
    pack(root)
    release = export(root, tmp_path / "release")
    assert len(list(release.rglob("manifest.json"))) == 3


def test_rejected_region_keeps_its_real_error(source, tmp_path):
    base, regional = tmp_path / "base", tmp_path / "regional"
    nac = fixture(tmp_path / "nac.tif", constant=200)
    build(source, base)
    regions.build(nac, base / "manifest.json", regional, SITE, 0, 0, 1000, 4)
    overlapping = {**SITE, "id": "other"}
    for _ in range(2):
        with pytest.raises(documents.InvalidDocument, match="overlapping"):
            regions.build(nac, base / "manifest.json", regional, overlapping, 0, 0, 1000, 4)
