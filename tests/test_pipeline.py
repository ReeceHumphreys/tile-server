import hashlib
import math
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform

from tests.conftest import ROOT, WAC, build, fixture, load, pixels, run, save
from tile_service import Workspace, cloudless, grids, heights, imagery, naip, regions, sources
from tile_service.catalog import LAYERS
from tile_service.cube import EARTH_RADIUS, MOON_RADIUS, Coverage, face_uv
from tile_service.height_index import HeightIndex
from tile_service.runtime import atomic_json, gdal_library

WS = Workspace(ROOT)

BASIS = [
    ([1, 0, 0], [0, 0, -1], [0, 1, 0]),
    ([-1, 0, 0], [0, 0, 1], [0, 1, 0]),
    ([0, 1, 0], [1, 0, 0], [0, 0, -1]),
    ([0, -1, 0], [1, 0, 0], [0, 0, 1]),
    ([0, 0, 1], [1, 0, 0], [0, 1, 0]),
    ([0, 0, -1], [-1, 0, 0], [0, 1, 0]),
]


def test_gdal_shared_library_is_discoverable():
    assert gdal_library().is_file()


def test_cube_orientation_poles_gutters_and_seams(source, tmp_path):
    output = tmp_path / "tiles"
    manifest = build(source, output, jobs=3)
    assert manifest["tileCount"] == 30
    assert manifest["pipelineVersion"] == 3
    for face, (normal, u_axis, v_axis) in enumerate(BASIS):
        image = pixels(output / f"{face}/0/0/0.png")
        assert image.shape == (36, 36, 4)
        assert np.all(image[:, :, 3] == 255)
        for row, col in [(0, 0), (10, 12), (18, 18), (25, 21), (35, 35)]:
            u = (col - 2 + 0.5) / 32 * 2 - 1
            v = 1 - (row - 2 + 0.5) / 32 * 2
            point = np.array(normal) + u * np.array(u_axis) + v * np.array(v_axis)
            point /= np.linalg.norm(point)
            expected = [127 - 100 * point[2], 127 + 100 * point[1], 127 + 100 * point[0]]
            assert np.all(np.abs(image[row, col, :3] - expected) < 4), (face, row, col)
    left, right, top = [
        pixels(output / path).astype(int) for path in ["0/1/0/0.png", "0/1/1/0.png", "0/1/0/1.png"]
    ]
    assert np.max(np.abs(left[:, 32:36] - right[:, :4])) <= 1
    assert np.max(np.abs(top[32:36] - left[:4])) <= 1


def test_worker_determinism_resume_and_incompatible_settings(source, tmp_path):
    a, b = tmp_path / "serial", tmp_path / "parallel"
    build(source, a, jobs=1)
    build(source, b, jobs=3)
    for path in a.rglob("*.png"):
        assert path.read_bytes() == (b / path.relative_to(a)).read_bytes()
    kept, missing = b / "0/0/0/0.png", b / "1/1/0/0.png"
    stamp = kept.stat().st_mtime_ns
    missing.unlink()
    (b / "manifest.json").unlink()
    build(source, b, jobs=2)
    assert kept.stat().st_mtime_ns == stamp
    assert missing.read_bytes() == (a / "1/1/0/0.png").read_bytes()
    with pytest.raises(RuntimeError, match="another source/settings"):
        build(source, b, max_level=0)


def test_nodata_and_wrong_sphere(tmp_path):
    source = fixture(tmp_path / "source.tif", nodata=True)
    output = tmp_path / "tiles"
    manifest = build(source, output, max_level=0)
    image = pixels(output / "0/0/0/0.png")
    assert image[5, 25, 3] == 0 and image[25, 5, 3] == 255
    assert manifest["source"]["nodata"] == 0
    fixture(source, radius=EARTH_RADIUS)
    with pytest.raises(RuntimeError, match="lunar sphere"):
        build(source, tmp_path / "wrong")
    assert not (tmp_path / "wrong/build-state.json").exists()


def test_polar_cache_has_no_block_boundary_holes(source, tmp_path):
    output = tmp_path / "tiles"
    build(source, output, tile_size=128, max_level=3, keep_cache=True, jobs=3)
    for face in [2, 3]:
        assert np.all(pixels(output / f".cache/{face}.tif")[:, :, 3] == 255)


@pytest.mark.parametrize("jobs", [0, 7, 1.5])
def test_invalid_options_do_not_create_outputs(source, tmp_path, jobs):
    output = tmp_path / "tiles"
    with pytest.raises(RuntimeError, match="Jobs"):
        build(source, output, jobs=jobs)
    assert not output.exists()
    assert "unrecognized arguments" in run(
        "build", "moon/wac", "--source", source, "--output", output, "--nonsense", ok=False
    )
    assert "Unknown target" in run("build", "moon/nothing", "--dry-run", ok=False)
    assert not output.exists()


def test_resumable_download_receipts_and_corruption(tmp_path):
    body = b"II*\0fixture-data"
    origin, output = tmp_path / "origin.tif", tmp_path / "download.tif"
    origin.write_bytes(body)
    settings = {
        **load(ROOT / "datasets/lroc-wac-100m.json"),
        "sourceUrl": origin.as_uri(),
        "expectedBytes": len(body),
    }
    Path(str(output) + ".part").write_bytes(body[:5])
    assert sources.ensure(settings, output) == output
    assert output.read_bytes() == body
    assert load(str(output) + ".receipt.json")["sha256"] == hashlib.sha256(body).hexdigest()
    origin.unlink()
    sources.ensure(settings, output)
    output.write_bytes(bytes(len(body)))
    with pytest.raises(RuntimeError, match="does not match"):
        sources.ensure(settings, output)
    origin.write_bytes(b"<html>!!")
    bad = tmp_path / "bad.tif"
    with pytest.raises(RuntimeError, match="not TIFF"):
        sources.ensure({**settings, "expectedBytes": 8}, bad)
    assert not bad.exists() and not Path(str(bad) + ".receipt.json").exists()


def test_interrupted_build_preserves_completed_work(source, tmp_path):
    output = tmp_path / "tiles"
    command = [
        sys.executable,
        "-u",
        "-m",
        "tile_service",
        "build",
        "moon/wac",
        "--source",
        str(source),
        "--output",
        str(output),
        "--max-level",
        "3",
        "--jobs",
        "2",
    ]
    with subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    ) as child:
        sent = False
        for line in child.stdout:
            if "Face 0 ready" in line:
                child.send_signal(signal.SIGINT)
                sent = True
                break
        child.communicate(timeout=30)
        assert sent and child.returncode != 0
    assert not (output / "manifest.json").exists()
    kept = output / "0/0/0/0.png"
    stamp = kept.stat().st_mtime_ns
    imagery.build(source, output, WAC, max_level=3, jobs=3)
    assert kept.stat().st_mtime_ns == stamp


@pytest.mark.skipif(
    not (ROOT / "data/raw/moon/ldem_16_uint.tif").exists()
    or not (ROOT / "public/data/moon/elevation/manifest.json").exists(),
    reason="local LOLA source unavailable",
)
def test_lola_conversion_matches_existing_grid(tmp_path):
    output = tmp_path / "elevation"
    entry = load(ROOT / "datasets/moon-lola-ldem16.json")
    grids.lola(sources.ensure(entry, WS.source_path(entry["sourceFile"])), output, entry)
    expected = load(ROOT / "public/data/moon/elevation/manifest.json")
    assert load(output / "manifest.json") == expected
    assert (
        hashlib.sha256((output / "heights.u16").read_bytes()).hexdigest() == expected["dataSha256"]
    )


def test_regional_imagery_matching_borders_resume_and_failed_publication(source, tmp_path):
    base, output = tmp_path / "base", tmp_path / "regional"
    build(source, base)
    nac = fixture(tmp_path / "nac.tif", constant=240)
    config = {
        "id": "test-site",
        "name": "Test landing site",
        "sourcePage": "https://example.test/source",
        "sourceUrl": "https://example.test/nac.tif",
        "credit": "Synthetic",
        "nativeResolutionMeters": 0.5,
    }

    def region(output=output, max_level=12):
        return regions.build(nac, base / "manifest.json", output, config, 0, 0, 1000, max_level)

    region()
    catalog = load(output / "catalog.json")
    assert catalog["baseMaxLevel"] == 1 and len(catalog["sites"]) == 1
    keys = set(catalog["sites"][0]["tiles"])
    assert 10 < len(keys) < 1000
    for key in keys:
        f, level, x, y = map(int, key.split("/"))
        if level > 2:
            assert f"{f}/{level - 1}/{x >> 1}/{y >> 1}" in keys
    path = output / "test-site/0/12/2048/2048.png"
    stamp, image = path.stat().st_mtime_ns, pixels(path)
    under = pixels(base / "0/1/1/1.png")
    matched = image[33, 2, 0]
    assert abs(float(matched) - np.mean(under[33, 1, :3])) < 8
    assert image[33, 2, 1] == matched == image[33, 2, 2]
    assert np.all(image[:, :, 3] == 255)
    assert pixels(output / "test-site/0/12/2049/2049.png")[18, 18, 0] < 200
    region()
    assert path.stat().st_mtime_ns == stamp
    edge = next(key for key in keys if key.startswith("0/12/") and key != "0/12/2048/2048")
    missing = output / "test-site" / (edge + ".png")
    missing.unlink()
    region()
    assert missing.exists() and path.stat().st_mtime_ns == stamp
    with pytest.raises(RuntimeError, match="another source/settings"):
        region(max_level=11)
    # Packing the base adds archive references; the region must still resume.
    manifest = load(base / "manifest.json")
    atomic_json(base / "manifest.json", {**manifest, "storage": {"type": "pmtiles"}})
    region()
    (base / "0/1/0/0.png").unlink()
    failed = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="Base tile missing"):
        region(output=failed)
    assert not (failed / "catalog.json").exists()


def test_numeric_heights_gutters_bounds_and_paged_refinements(tmp_path):
    width, height = 128, 64
    y, x = np.mgrid[:height, :width]
    values = (
        20000
        + np.floor(
            800 * np.sin((x + 0.5) / width * math.tau) * np.cos((y + 0.5) / height * math.pi) + 0.5
        )
    ).astype("<u2")
    raw = values.tobytes()
    (tmp_path / "heights.u16").write_bytes(raw)
    config = tmp_path / "source.json"
    save(
        config,
        {
            "body": "Moon",
            "format": "uint16-le",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "radiusMeters": 1737400,
            "width": width,
            "height": height,
            "data": "heights.u16",
            "dataSha256": hashlib.sha256(raw).hexdigest(),
            "credit": "analytic",
            "sourcePage": "fixture",
        },
    )
    output = tmp_path / "heights"
    manifest = heights.build(config, output, tile_size=16, max_level=1)
    assert len(manifest["tiles"]) == 30

    def read(key):
        return np.frombuffer(
            (output / manifest["tiles"][key]["path"]).read_bytes(), dtype="<u2"
        ).reshape(20, 20)

    assert np.array_equal(read("0/1/0/0")[:, 16:], read("0/1/1/0")[:, :4])
    for key, info in manifest["tiles"].items():
        f, level, x, y = map(int, key.split("/"))
        if level:
            parent = manifest["tiles"][f"{f}/{level - 1}/{x >> 1}/{y >> 1}"]
            assert parent["lower"] <= info["lower"] <= info["upper"] <= parent["upper"]
    kept = output / manifest["tiles"]["0/0/0/0"]["path"]
    stamp = kept.stat().st_mtime_ns
    heights.build(config, output, tile_size=16, max_level=1)
    assert kept.stat().st_mtime_ns == stamp
    source = fixture(tmp_path / "source.tif", constant=240, height=True)
    heights.add_region(
        output,
        source,
        "test-height",
        0,
        0,
        1000,
        max_level=10,
        input_offset=-500,
        credit="test",
        overview={"latitudeDeg": 0, "longitudeDeg": 0, "radiusMeters": 20000, "maxLevel": 9},
        feather=1000,
    )
    manifest = load(output / "manifest.json")
    assert np.all(
        np.frombuffer((output / "test-height/0/10/512/512.u16").read_bytes(), dtype="<u2") == 19480
    )
    assert manifest["pages"] and len(manifest["tiles"]) == 30
    for page in manifest["pages"].values():
        assert load(output / page["path"])["tiles"]
        assert hashlib.sha256((output / page["path"]).read_bytes()).hexdigest() == page["sha256"]
    snapshot = (output / "manifest.json").read_bytes()
    heights.build(config, output, tile_size=16, max_level=1)
    assert (output / "manifest.json").read_bytes() == snapshot


def test_polar_vrt_coverage_and_replacement(source, tmp_path):
    paths = [
        fixture(tmp_path / f"q{q}.tif", polar_quadrant=q, constant=80 + q * 40) for q in range(4)
    ]
    vrt = tmp_path / "polar.vrt"
    sources.mosaic(paths, vrt, MOON_RADIUS)
    image = pixels(vrt)
    assert image.shape == (720, 1440, 3)
    for x, y, value in [(1000, 100, 80), (1000, 600, 120), (100, 600, 160), (100, 100, 200)]:
        assert image[y, x, 0] == value
    assert vrt.stat().st_size < 10000
    stamp = vrt.stat().st_mtime_ns
    sources.mosaic(paths, vrt, MOON_RADIUS)
    assert vrt.stat().st_mtime_ns == stamp
    base, output = tmp_path / "base", tmp_path / "regional"
    build(source, base)
    overview = {"latitudeDeg": -90, "longitudeDeg": 0, "radiusMeters": 44000, "maxLevel": 7}
    settings = {
        "id": "polar-test",
        "name": "Shackleton",
        "nativeResolutionMeters": 100,
        "sourcePage": "https://example.test/polar",
        "credit": "test",
        "components": [{"product": "q0", "sha256": "fixture"}],
        "overview": overview,
        "featherMeters": 4000,
    }

    def region(config):
        regions.build(vrt, base / "manifest.json", output, config, -89.67, 129.78, 16000, 9)

    region(settings)
    site = load(output / "catalog.json")["sites"][0]
    assert all(key.startswith("3/") for key in site["tiles"])
    plan = Coverage(-89.67, 129.78, 16000, MOON_RADIUS, 1, 9, overview=overview, feather=4000)
    assert site["tiles"] == [tile.key for tile in plan.tiles]
    assert len(plan.tiles) > len(Coverage(-89.67, 129.78, 16000, MOON_RADIUS, 1, 9).tiles)
    assert site["source"]["components"][0]["product"] == "q0" and site["featherMeters"] == 4000
    assert pixels(output / "polar-test/3/9/255/255.png").any()
    region({**settings, "id": "polar-test-v2", "replacesRegion": "polar-test"})
    assert [s["id"] for s in load(output / "catalog.json")["sites"]] == ["polar-test-v2"]
    assert (output / "polar-test/manifest.json").exists()


def test_polar_preflight_guards_and_apollo_catalogs(capsys):
    nac = load(ROOT / "datasets/south-pole-nac.json")["sites"][0]
    lola = load(ROOT / "datasets/south-pole-lola.json")["sites"][0]
    parts = nac["source"]["parts"]
    assert len(parts) == len({s["id"] for s in parts}) == 4
    for source in [*parts, lola["source"]]:
        assert source["sourceUrl"].startswith("https://") and source["expectedBytes"] > 0
        assert re.fullmatch(r'(W/)?"[a-f0-9-]+"', source["sourceETag"])
    WS.build("moon/south-pole-imagery", dry_run=True)
    WS.build("moon/south-pole-elevation", dry_run=True)
    info = capsys.readouterr().out
    assert "8.28 GB" in info and "3,465,285,714 bytes" in info and "radius 16000 m" in info
    assert "Planned addresses: at most" in info
    with pytest.raises(RuntimeError, match="20,000 tiles"):
        WS.build("moon/south-pole-imagery", max_level=16, dry_run=True)
    with pytest.raises(RuntimeError, match="1..16"):
        WS.build("moon/south-pole-elevation", max_level=0, dry_run=True)
    WS.build("moon/apollo", sites=("apollo12",), dry_run=True)
    WS.build("moon/apollo-elevation", sites=("apollo12-dtm",), dry_run=True)
    info = capsys.readouterr().out
    assert "apollo/apollo12.tif" in info and "apollo/apollo12-dtm.tif" in info
    with pytest.raises(RuntimeError, match="Unknown site"):
        WS.build("moon/apollo", sites=("unknown",), dry_run=True)
    with pytest.raises(RuntimeError, match="--site applies"):
        WS.build("moon/wac", sites=("apollo12",), dry_run=True)


def test_generic_mars_imagery_and_elevation(tmp_path):
    radius = 3389500
    source = fixture(tmp_path / "source.tif", radius=radius, constant=127)
    dataset = {**WAC, "id": "mars-test", "body": "Mars", "radiusMeters": radius}
    images = build(source, tmp_path / "images", dataset)
    assert images["body"] == "Mars" and images["radiusMeters"] == radius
    dem = fixture(tmp_path / "dem.tif", radius=radius, height=True, constant=100)
    grid, output = tmp_path / "grid", tmp_path / "heights"

    def prepare(out=grid, **options):
        settings = {"body": "Mars", "radius": radius, "width": 32, "height": 16, **options}
        return grids.warp(dem, out, **settings)

    manifest = prepare()
    assert manifest["body"] == "Mars" and manifest["minMeters"] == manifest["maxMeters"] == 100
    assert prepare() == manifest
    tiled = heights.build(grid / "manifest.json", output, tile_size=16, max_level=0)
    assert tiled["body"] == "Mars" and tiled["radiusMeters"] == radius and len(tiled["tiles"]) == 6
    assert tiled["minMeters"] == tiled["maxMeters"] == 100
    assert all(info["min"] == info["max"] == 100 for info in tiled["tiles"].values())
    with pytest.raises(RuntimeError, match="reference sphere"):
        prepare(tmp_path / "wrong", body="Moon", radius=MOON_RADIUS)
    with pytest.raises(RuntimeError, match="encoding range"):
        prepare(tmp_path / "range", input_offset=-10101)


def jpeg(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.empty((3, 256, 256), np.uint8)
    data[:] = np.array(color)[:, None, None]
    with rasterio.open(
        path, "w", driver="JPEG", width=256, height=256, count=3, dtype="uint8", quality=90
    ) as target:
        target.write(data)


def test_cloudless_stitch_resume_and_earth_cube(tmp_path, capsys):
    listed = load(ROOT / "datasets/s2cloudless-2020.json")
    assert listed["body"] == "Earth" and listed["radiusMeters"] == 6378137
    WS.build("earth/s2cloudless-2020-z7", dry_run=True)
    assert "32768 requests" in capsys.readouterr().out
    root = tmp_path / "tiles"
    jpeg(root / "0/0/0.jpg", [10, 20, 30])
    jpeg(root / "0/0/1.jpg", [200, 180, 160])
    output = tmp_path / "mosaic.tif"
    template = root.as_uri() + "/{matrix}/{row}/{col}.jpg"

    def mosaic(zoom=0, jobs=2):
        cloudless.mosaic(output, zoom=zoom, template=template, jobs=jobs)
        return capsys.readouterr().out

    assert "2 x 1 tiles" in mosaic()
    image = pixels(output)
    assert image.shape == (256, 512, 3)
    assert np.all(np.abs(image[0, 0].astype(int) - [10, 20, 30]) <= 2)
    assert np.all(np.abs(image[128, 400].astype(int) - [200, 180, 160]) <= 2)
    cache = tmp_path / "mosaic-tiles/0/0/0.jpg"
    stamp, cache_stamp = output.stat().st_mtime_ns, cache.stat().st_mtime_ns
    assert "no download needed" in mosaic()
    assert output.stat().st_mtime_ns == stamp
    with pytest.raises(RuntimeError, match="another download"):
        mosaic(zoom=1)
    output.unlink()
    Path(str(output) + ".receipt.json").unlink()
    mosaic()
    assert cache.stat().st_mtime_ns == cache_stamp
    manifest = build(output, tmp_path / "pyramid", listed, max_level=0)
    assert manifest["body"] == "Earth" and "CC BY-NC-SA 4.0" in manifest["source"]["credit"]
    assert np.all(pixels(tmp_path / "pyramid/0/0/0/0.png")[:, :, 3] == 255)
    for options in [
        {"zoom": 8},
        {"jobs": 0},
        {"jobs": 33},
        {"template": "https://example.test/tile.jpg"},
    ]:
        with pytest.raises(RuntimeError):
            cloudless.mosaic(tmp_path / "rejected/m.tif", **options)
    assert not (tmp_path / "rejected").exists()


def test_cloudless_palette_tile_preserves_white(tmp_path):
    root = tmp_path / "tiles"
    jpeg(root / "0/0/0.jpg", [10, 20, 30])
    with rasterio.open(
        root / "0/0/1.jpg",
        "w",
        driver="PNG",
        width=256,
        height=256,
        count=1,
        dtype="uint8",
        photometric="palette",
    ) as target:
        target.write_colormap(1, {0: (255, 255, 255, 255)})
        target.write(np.zeros((1, 256, 256), np.uint8))
    output = tmp_path / "mosaic.tif"
    cloudless.mosaic(output, zoom=0, template=root.as_uri() + "/{matrix}/{row}/{col}.jpg")
    assert list(pixels(output)[128, 400]) == [255, 255, 255]


def earth_grid(source, out):
    return grids.warp(
        source, out, body="Earth", radius=EARTH_RADIUS, width=8, height=4, geographic=True
    )


def test_earth_elevation_trenches_nodata_and_tiles(tmp_path, capsys):
    WS.build("earth/elevation", dry_run=True)
    info = capsys.readouterr().out
    assert "465,969,062 bytes" in info and "16384 x 8192" in info
    for name, value, nodata, expected in [
        ("trench", -12000, False, -10000),
        ("land", 100, False, 100),
        ("empty", 0, True, 0),
    ]:
        path, grid = tmp_path / (name + ".tif"), tmp_path / name
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=4,
            height=2,
            count=1,
            dtype="float32",
            crs="EPSG:9518",
            nodata=-99999.0,
            transform=rasterio.transform.from_bounds(-180, -90, 180, 90, 4, 2),
        ) as target:
            target.write(np.full((1, 2, 4), -99999 if nodata else value, np.float32))
        manifest = earth_grid(path, grid)
        assert manifest["body"] == "Earth" and manifest["radiusMeters"] == 6378137
        assert manifest["minMeters"] == manifest["maxMeters"] == expected
    output = tmp_path / "tiles"
    tiled = heights.build(tmp_path / "trench/manifest.json", output, tile_size=16, max_level=0)
    assert tiled["minMeters"] == tiled["maxMeters"] == -10000
    assert all(info["min"] == info["max"] == -10000 for info in tiled["tiles"].values())
    source = fixture(tmp_path / "sphere.tif", radius=EARTH_RADIUS, height=True, constant=100)
    with pytest.raises(RuntimeError, match="geographic"):
        earth_grid(source, tmp_path / "wrong")


@pytest.mark.parametrize(
    "target,size,filename",
    [
        ("mars/elevation", 2125771142, "Mars_MGS_MOLA_DEM_mosaic_global_463m.tif"),
        ("mars/viking", 12742411029, "Mars_Viking_MDIM21_ClrMosaic_global_232m.tif"),
    ],
)
def test_mars_catalog_integrity(target, size, filename, capsys):
    listed = load(ROOT / "datasets" / WS.targets()[target]["source"]["dataset"])
    assert listed["body"] == "Mars" and listed["radiusMeters"] == 3396190
    assert listed["expectedBytes"] == size
    WS.build(target, dry_run=True)
    info = capsys.readouterr().out
    assert f"{size:,} bytes" in info and filename in info


def test_catalog_targets_reference_real_datasets():
    for name, spec in WS.targets().items():
        assert spec["layer"] in LAYERS, name
        for key in ("sites",):
            if key in spec:
                assert (ROOT / "datasets" / spec[key]).exists(), (name, spec[key])
        if "dataset" in spec.get("source", {}):
            assert (ROOT / "datasets" / spec["source"]["dataset"]).exists(), name
        for key in ("base", "grid"):
            if key in spec:
                assert spec[key] in WS.targets(), (name, spec[key])
        WS.build(name, dry_run=True)


def test_earth_sites_keep_geodetic_angles(capsys):
    assert [s["id"] for s in load(ROOT / "datasets/earth-sites.json")["sites"]] == [
        "starbase",
        "ksc",
    ]
    WS.build("earth/sites", sites=("ksc",), dry_run=True)
    info = capsys.readouterr().out
    assert "Kennedy Space Center" in info and "LC-39A" in info and "max level 16" in info


def test_naip_warp_keeps_geodetic_position(tmp_path):
    """A synthetic UTM pixel at KSC lands within 1.5 m of its geodetic position."""
    lon, lat = -80.604333, 28.608389
    pixel = 1.0
    xs, ys = transform(CRS.from_epsg(4269), CRS.from_epsg(26917), [lon], [lat])
    n = 32
    col = n // 2
    src_transform = rasterio.transform.Affine(
        pixel,
        0,
        xs[0] - (col + 0.5) * pixel,
        0,
        -pixel,
        ys[0] + (col + 0.5) * pixel,
    )
    image = np.full((3, n, n), 30, np.uint8)
    image[:, col, col] = 220
    path = tmp_path / "self-test.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=n,
        height=n,
        count=3,
        dtype="uint8",
        crs=CRS.from_epsg(26917),
        transform=src_transform,
    ) as dataset:
        dataset.write(image)
    grid, width, height = naip._grid(lat, lon, 12.0, EARTH_RADIUS, pixel)
    acc, _ = naip._mosaic([path], grid, width, height, EARTH_RADIUS)
    expected_x = math.radians(lon) * EARTH_RADIUS
    expected_y = math.radians(lat) * EARTH_RADIUS
    expected_col = (expected_x - grid.c) / grid.a - 0.5
    expected_row = (expected_y - grid.f) / grid.e - 0.5
    row, column = np.unravel_index(int(np.argmax(acc[0])), acc[0].shape)
    shift = math.hypot((column - expected_col) * pixel, (row - expected_row) * pixel)
    assert shift < 1.5 and int(acc[0, row, column]) >= 100


def test_earth_poles_are_one_height(tmp_path):
    path, grid = tmp_path / "poles.tif", tmp_path / "poles"
    values = np.array([[0, 1000, 2000, 3000], [10, 20, 30, 40]], np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=rasterio.transform.from_bounds(-180, -90, 180, 90, 4, 2),
    ) as target:
        target.write(values[None])
    earth_grid(path, grid)
    encoded = np.frombuffer((grid / "heights.u16").read_bytes(), dtype="<u2").reshape(4, 8)
    assert len(set(encoded[0].tolist())) == 1
    assert len(set(encoded[-1].tolist())) == 1
    assert encoded[0, 0] != encoded[-1, 0]


def test_pinned_source_keeps_a_mismatched_file(tmp_path):
    source = tmp_path / "ldem.tif"
    source.write_bytes(b"not a dem")
    entry = load(ROOT / "datasets/moon-lola-ldem16.json")
    with pytest.raises(RuntimeError, match="checksum"):
        sources.ensure(entry, source)
    assert source.read_bytes() == b"not a dem"
    assert not Path(str(source) + ".receipt.json").exists()


def test_face_uv_grid_matches_single_vectors():
    vectors = np.array(
        [
            [1, 0, 0],
            [-1, 0, 0],
            [0, 1, 0],
            [0, -1, 0],
            [0, 0, 1],
            [0, 0, -1],
            [1, 1, 1],
            [-0.2, 0.1, -0.9],
        ],
        dtype=float,
    )
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    faces, us, vs = face_uv(vectors.T.reshape(3, 2, 4))
    assert list(faces.ravel()[:6]) == [0, 1, 2, 3, 4, 5]
    assert us[0, 0] == vs[0, 0] == pytest.approx(0.5)
    for index, vector in enumerate(vectors):
        face, u, v = face_uv(vector)
        y, x = divmod(index, 4)
        assert faces[y, x] == face
        assert us[y, x] == pytest.approx(u)
        assert vs[y, x] == pytest.approx(v)


def test_paged_coarse_samples_match_the_inline_base(tmp_path):
    width, height = 32, 16
    y, x = np.mgrid[:height, :width]
    raw = (20000 + (x * 17 + y * 3)).astype("<u2").tobytes()
    (tmp_path / "heights.u16").write_bytes(raw)
    config = tmp_path / "source.json"
    save(
        config,
        {
            "body": "Moon",
            "format": "uint16-le",
            "scaleMeters": 0.5,
            "offsetMeters": -10000,
            "radiusMeters": 1737400,
            "width": width,
            "height": height,
            "data": "heights.u16",
            "dataSha256": hashlib.sha256(raw).hexdigest(),
            "credit": "analytic",
            "sourcePage": "fixture",
        },
    )
    base = tmp_path / "base"
    heights.build(config, base, tile_size=16, max_level=1)
    paged = tmp_path / "paged"
    shutil.copytree(base, paged)
    assert HeightIndex.open(paged).publish(page_level=0)["pages"]
    fine = fixture(tmp_path / "fine.tif", height=True, constant=40)
    for root in (base, paged):
        heights.add_region(root, fine, "patch", 0, 0, 500, max_level=2)

    def files(root):
        return {
            path.relative_to(root / "patch").as_posix(): path.read_bytes()
            for path in (root / "patch").rglob("*.u16")
        }

    direct, copied = files(base), files(paged)
    shared = {key for key in direct.keys() & copied.keys() if "/2/" in key}
    assert shared
    assert all(direct[key] == copied[key] for key in shared)
    samples = np.concatenate([np.frombuffer(direct[key], dtype="<u2") for key in shared])
    assert np.count_nonzero(samples != (40 + 10000) * 2) > 0


def test_pinned_checksum_and_receipt_precede_rename(tmp_path, monkeypatch):
    official = tmp_path / "ETOPO.tif"
    body = b"II*\x00" + b"x" * 12
    bad = b"II*\x00" + b"y" * 12
    digest = hashlib.sha256(body).hexdigest()
    partial = Path(str(official) + ".part")
    receipt = Path(str(official) + ".receipt.json")
    entry = {
        "sourceUrl": "https://example.test/etopo.tif",
        "expectedBytes": len(body),
        "sha256": digest,
        "sourceFile": str(official),
    }

    def refuse_download(*_args, **_kwargs):
        raise AssertionError("downloaded")

    monkeypatch.setattr(sources, "download", refuse_download)
    partial.write_bytes(bad)
    with pytest.raises(RuntimeError, match="checksum"):
        sources.ensure(entry)
    assert partial.read_bytes() == bad and not official.exists() and not receipt.exists()
    partial.write_bytes(body)
    real_replace = os.replace

    def fail_source_rename(src, dst):
        if str(src).endswith(".part"):
            raise OSError("rename failed")
        real_replace(src, dst)

    monkeypatch.setattr(sources.os, "replace", fail_source_rename)
    with pytest.raises(OSError, match="rename failed"):
        sources.ensure(entry)
    assert load(receipt)["sha256"] == digest and not official.exists() and partial.exists()
    monkeypatch.setattr(sources.os, "replace", real_replace)
    sources.ensure(entry)
    assert official.read_bytes() == body and not partial.exists()
    receipt.write_text(receipt.read_text().replace(digest, "0" * 64))
    with pytest.raises(RuntimeError, match="receipt"):
        sources.ensure(entry)
    assert official.read_bytes() == body


def test_second_interrupt_uses_the_default_handler(monkeypatch):
    from tile_service import runtime

    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)
    runtime.stop.clear()
    killed = []
    monkeypatch.setattr(runtime.os, "kill", lambda pid, signum: killed.append((pid, signum)))
    try:
        runtime.install_signals()
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
        assert runtime.stop.is_set() and killed == []
        handler(signal.SIGINT, None)
        assert killed == [(os.getpid(), signal.SIGINT)]
        assert signal.getsignal(signal.SIGINT) is signal.SIG_DFL
    finally:
        runtime.stop.clear()
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


def test_a_workspace_adds_its_own_targets_and_sources(tmp_path):
    """A user workspace defines datasets, sites and targets; nothing in the package changes."""
    upstream = tmp_path / "upstream"
    globe = fixture(upstream / "globe.tif")
    detail = fixture(upstream / "detail.tif", constant=210)

    def download(path, **fields):
        return {
            "sourceUrl": path.as_uri(),
            "expectedBytes": path.stat().st_size,
            "credit": "Synthetic",
            **fields,
        }

    ws = Workspace(tmp_path / "project")
    save(
        ws.root / "datasets/test-globe.json",
        {**WAC, "id": "test-globe", **download(globe), "sourceFile": "test/globe.tif"},
    )
    save(
        ws.root / "datasets/test-sites.json",
        {
            "sites": [
                {
                    "id": "spot",
                    "name": "Spot",
                    "latitudeDeg": 0,
                    "longitudeDeg": 0,
                    "maxLevel": 9,
                    "nativeResolutionMeters": 1,
                    "sourcePage": "https://example.test/spot",
                    **download(detail, id="spot"),
                }
            ]
        },
    )
    save(
        ws.root / "datasets/catalog.json",
        {
            "targets": {
                "test/globe": {
                    "layer": "imagery",
                    "source": {"type": "download", "dataset": "test-globe.json"},
                },
                "test/spots": {
                    "layer": "imagery-regions",
                    "base": "test/globe",
                    "sites": "test-sites.json",
                    "source": {"type": "download", "raw": "test/spots"},
                    "radiusMeters": 1500,
                },
            }
        },
    )
    assert {"test/globe", "test/spots", "moon/wac"} <= set(ws.targets())
    assert ws.status("test/spots") == "-"
    ws.build("test/globe", max_level=1)
    ws.build("test/spots")
    assert ws.status("test/globe") == ws.status("test/spots") == "built"
    assert (ws.sources / "test/globe.tif").exists() and (
        ws.sources / "test/spots/spot.tif"
    ).exists()
    site = load(ws.builds / "test/spots/catalog.json")["sites"][0]
    assert site["source"]["credit"] == "Synthetic" and site["maxLevel"] == 9
    release = ws.release("releases/v1")
    assert release == (ws.root / "releases/v1").resolve()
    assert len(list(release.rglob("*.pmtiles"))) == 7


def test_cached_faces_get_overviews_before_their_final_name(source, tmp_path, monkeypatch):
    replace, seen = os.replace, []

    def checked(src, dst):
        if str(src).endswith(".tif.tmp"):
            with rasterio.open(src) as cache:
                seen.append(cache.overviews(1))
        replace(src, dst)

    monkeypatch.setattr(imagery.os, "replace", checked)
    build(source, tmp_path / "out")
    assert len(seen) == 6 and all(seen)


def test_failed_imagery_build_can_be_retried(source, tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(imagery, "_extract", lambda *args: 1 / 0)
        with pytest.raises(ZeroDivisionError):
            build(source, tmp_path / "out")
    assert build(source, tmp_path / "out")


def test_all_refuses_single_target_options(tmp_path):
    with pytest.raises(RuntimeError, match="single target"):
        Workspace(tmp_path).build("all", source="x.tif", dry_run=True)
