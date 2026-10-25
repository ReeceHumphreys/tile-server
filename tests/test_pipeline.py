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

from pipeline.jobs import plan_coverage
from pipeline.runtime import gdal_library
from tests.conftest import ROOT, build, fixture, load, pixels, run, save

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
    manifest = build(source, output, "--jobs", 3)
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
    build(source, a, "--jobs", 1)
    build(source, b, "--jobs", 3)
    for path in a.rglob("*.png"):
        assert path.read_bytes() == (b / path.relative_to(a)).read_bytes()
    kept, missing = b / "0/0/0/0.png", b / "1/1/0/0.png"
    stamp = kept.stat().st_mtime_ns
    missing.unlink()
    (b / "manifest.json").unlink()
    build(source, b, "--jobs", 2)
    assert kept.stat().st_mtime_ns == stamp
    assert missing.read_bytes() == (a / "1/1/0/0.png").read_bytes()
    assert "another source/settings" in run(
        "tiles", "--source", source, "--output", b, "--tile-size", 32, "--max-level", 0, ok=False
    )


def test_nodata_and_wrong_sphere(tmp_path):
    source = fixture(tmp_path / "source.tif", "--nodata")
    output = tmp_path / "tiles"
    manifest = build(source, output, "--max-level", 0)
    image = pixels(output / "0/0/0/0.png")
    assert image[5, 25, 3] == 0 and image[25, 5, 3] == 255
    assert manifest["source"]["nodata"] == 0
    fixture(source, "--earth")
    assert "lunar sphere" in run(
        "tiles", "--source", source, "--output", tmp_path / "wrong", ok=False
    )
    assert not (tmp_path / "wrong/build-state.json").exists()


def test_polar_cache_has_no_block_boundary_holes(source, tmp_path):
    output = tmp_path / "tiles"
    build(source, output, "--tile-size", 128, "--max-level", 3, "--keep-cache", "--jobs", 3)
    for face in [2, 3]:
        assert np.all(pixels(output / f".cache/{face}.tif")[:, :, 3] == 255)


@pytest.mark.parametrize("jobs", ["0", "7", "1.5"])
def test_invalid_options_do_not_create_outputs(source, tmp_path, jobs):
    output = tmp_path / "tiles"
    run("tiles", "--source", source, "--output", output, "--jobs", jobs, ok=False)
    assert not output.exists()
    assert "Unknown option" in run(
        "tiles", "--source", source, "--output", output, "--nonsense", 1, ok=False
    )


def test_resumable_download_receipts_and_corruption(tmp_path):
    body = b"II*\0fixture-data"
    origin, output, config = [
        tmp_path / name for name in ["origin.tif", "download.tif", "config.json"]
    ]
    origin.write_bytes(body)
    settings = {
        **load(ROOT / "datasets/lroc-wac-100m.json"),
        "sourceUrl": origin.as_uri(),
        "expectedBytes": len(body),
    }
    save(config, settings)
    Path(str(output) + ".part").write_bytes(body[:5])
    run("fetch", "--config", config, "--output", output)
    assert output.read_bytes() == body
    assert load(str(output) + ".receipt.json")["sha256"] == hashlib.sha256(body).hexdigest()
    origin.unlink()
    run("fetch", "--config", config, "--output", output)
    output.write_bytes(bytes(len(body)))
    assert "does not match" in run("fetch", "--config", config, "--output", output, ok=False)
    origin.write_bytes(b"<html>!!")
    save(config, {**settings, "expectedBytes": 8})
    bad = tmp_path / "bad.tif"
    assert "not TIFF" in run("fetch", "--config", config, "--output", bad, ok=False)
    assert not bad.exists() and not Path(str(bad) + ".receipt.json").exists()


def test_interrupted_build_preserves_completed_work(source, tmp_path):
    output = tmp_path / "tiles"
    command = [
        sys.executable,
        "-u",
        "-m",
        "pipeline",
        "tiles",
        "--source",
        str(source),
        "--output",
        str(output),
        "--tile-size",
        "128",
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
    build(source, output, "--tile-size", 128, "--max-level", 3, "--jobs", 3)
    assert kept.stat().st_mtime_ns == stamp


@pytest.mark.skipif(
    not (ROOT / "data/raw/moon/ldem_16_uint.tif").exists()
    or not (ROOT / "public/data/moon/elevation/manifest.json").exists(),
    reason="local LOLA source unavailable",
)
def test_lola_conversion_matches_existing_grid(tmp_path):
    output = tmp_path / "elevation"
    run("elevation", "--output", output)
    expected = load(ROOT / "public/data/moon/elevation/manifest.json")
    assert load(output / "manifest.json") == expected
    assert (
        hashlib.sha256((output / "heights.u16").read_bytes()).hexdigest() == expected["dataSha256"]
    )


def test_regional_imagery_matching_borders_resume_and_failed_publication(source, tmp_path):
    base, output = tmp_path / "base", tmp_path / "regional"
    build(source, base)
    nac = fixture(tmp_path / "nac.tif", "--constant", 240)
    config = tmp_path / "nac.json"
    save(
        config,
        {
            "id": "test-site",
            "name": "Test landing site",
            "sourcePage": "https://example.test/source",
            "sourceUrl": "https://example.test/nac.tif",
            "credit": "Synthetic",
            "nativeResolutionMeters": 0.5,
        },
    )
    args = [
        "region",
        "--config",
        config,
        "--source",
        nac,
        "--base",
        base / "manifest.json",
        "--output",
        output,
        "--latitude",
        0,
        "--longitude",
        0,
        "--radius-meters",
        1000,
        "--max-level",
        12,
    ]
    run(*args)
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
    run(*args)
    assert path.stat().st_mtime_ns == stamp
    edge = next(key for key in keys if key.startswith("0/12/") and key != "0/12/2048/2048")
    missing = output / "test-site" / (edge + ".png")
    missing.unlink()
    run(*args)
    assert missing.exists() and path.stat().st_mtime_ns == stamp
    assert "another source/settings" in run(*args[:-1], 11, ok=False)
    (base / "0/1/0/0.png").unlink()
    failed = tmp_path / "failed"
    run(*[failed if value == output else value for value in args], ok=False)
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
    args = [
        "height-tiles",
        "--source",
        config,
        "--output",
        output,
        "--tile-size",
        16,
        "--max-level",
        1,
    ]
    run(*args)
    manifest = load(output / "manifest.json")
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
    run(*args)
    assert kept.stat().st_mtime_ns == stamp
    source = fixture(tmp_path / "source.tif", "--constant", 240, "--height")
    coverage = tmp_path / "coverage.json"
    save(
        coverage,
        {
            "overview": {"latitudeDeg": 0, "longitudeDeg": 0, "radiusMeters": 20000, "maxLevel": 9},
            "featherMeters": 1000,
        },
    )
    run(
        "height-region",
        "--source",
        source,
        "--output",
        output,
        "--id",
        "test-height",
        "--coverage-config",
        coverage,
        "--latitude",
        0,
        "--longitude",
        0,
        "--radius-meters",
        1000,
        "--max-level",
        10,
        "--input-scale",
        1,
        "--input-offset",
        -500,
        "--credit",
        "test",
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
    run(*args)
    assert (output / "manifest.json").read_bytes() == snapshot


def test_polar_vrt_coverage_and_replacement(source, tmp_path):
    paths = [
        fixture(tmp_path / f"q{q}.tif", "--polar-quadrant", q, "--constant", 80 + q * 40)
        for q in range(4)
    ]
    listing, vrt = tmp_path / "sources.json", tmp_path / "polar.vrt"
    save(listing, list(map(str, paths)))
    run("mosaic", "--sources", listing, "--output", vrt)
    image = pixels(vrt)
    assert image.shape == (720, 1440, 3)
    for x, y, value in [(1000, 100, 80), (1000, 600, 120), (100, 600, 160), (100, 100, 200)]:
        assert image[y, x, 0] == value
    assert vrt.stat().st_size < 10000
    stamp = vrt.stat().st_mtime_ns
    run("mosaic", "--sources", listing, "--output", vrt)
    assert vrt.stat().st_mtime_ns == stamp
    base, output, config = tmp_path / "base", tmp_path / "regional", tmp_path / "polar.json"
    build(source, base)
    overview = {"latitudeDeg": -90, "longitudeDeg": 0, "radiusMeters": 44000, "maxLevel": 7}
    detail = {"latitudeDeg": -89.67, "longitudeDeg": 129.78, "radiusMeters": 16000, "maxLevel": 9}
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
    save(config, settings)
    args = [
        "region",
        "--config",
        config,
        "--source",
        vrt,
        "--base",
        base / "manifest.json",
        "--output",
        output,
        "--latitude",
        -89.67,
        "--longitude",
        129.78,
        "--radius-meters",
        16000,
        "--max-level",
        9,
    ]
    run(*args)
    site = load(output / "catalog.json")["sites"][0]
    assert all(key.startswith("3/") for key in site["tiles"])
    assert site["tiles"] == [address.key for address in plan_coverage(detail, overview, 2)]
    assert site["source"]["components"][0]["product"] == "q0" and site["featherMeters"] == 4000
    assert pixels(output / "polar-test/3/9/255/255.png").any()
    save(config, {**settings, "id": "polar-test-v2", "replacesRegion": "polar-test"})
    run(*args)
    assert [s["id"] for s in load(output / "catalog.json")["sites"]] == ["polar-test-v2"]
    assert (output / "polar-test/manifest.json").exists()


def test_polar_preflight_guards_and_apollo_catalogs():
    catalog = load(ROOT / "datasets/south-pole.json")
    assert len(catalog["imagery"]) == len({s["id"] for s in catalog["imagery"]}) == 4
    for source in [*catalog["imagery"], catalog["elevation"]]:
        assert source["sourceUrl"].startswith("https://") and source["expectedBytes"] > 0
        assert re.fullmatch(r'(W/)?"[a-f0-9-]+"', source["sourceETag"])
    info = run("polar", "--info")
    assert "11.74 GB" in info and "16 km radius" in info
    assert "20,000 tiles" in run("polar", "--max-level", 16, ok=False)
    assert "1..16" in run("polar", "--max-level", 0, "--info", ok=False)
    run("polar", "--imagery-only", "--elevation-only", ok=False)
    assert "Apollo 12" in run("apollo", "--site", "apollo12", "--info")
    assert "Apollo 12" in run("apollo", "--site", "apollo12", "--elevation", "--info")
    assert "Unknown Apollo" in run("apollo", "--site", "unknown", "--info", ok=False)


def test_generic_mars_imagery_and_elevation(tmp_path):
    radius = 3389500
    source = fixture(tmp_path / "source.tif", "--body-radius", radius, "--constant", 127)
    config = tmp_path / "mars.json"
    save(
        config,
        {
            **load(ROOT / "datasets/lroc-wac-100m.json"),
            "id": "mars-test",
            "body": "Mars",
            "radiusMeters": radius,
        },
    )
    images = build(source, tmp_path / "images", "--config", config)
    assert images["body"] == "Mars" and images["radiusMeters"] == radius
    dem = fixture(tmp_path / "dem.tif", "--body-radius", radius, "--height", "--constant", 100)
    grid, output = tmp_path / "grid", tmp_path / "heights"
    args = [
        "prepare-elevation",
        "--source",
        dem,
        "--output",
        grid,
        "--body",
        "Mars",
        "--body-radius",
        radius,
        "--width",
        32,
        "--height",
        16,
    ]
    run(*args)
    manifest = load(grid / "manifest.json")
    assert manifest["body"] == "Mars" and manifest["minMeters"] == manifest["maxMeters"] == 100
    run(
        "height-tiles",
        "--source",
        grid / "manifest.json",
        "--output",
        output,
        "--tile-size",
        16,
        "--max-level",
        0,
    )
    tiled = load(output / "manifest.json")
    assert tiled["body"] == "Mars" and tiled["radiusMeters"] == radius and len(tiled["tiles"]) == 6
    assert tiled["minMeters"] == tiled["maxMeters"] == 100
    assert all(info["min"] == info["max"] == 100 for info in tiled["tiles"].values())
    assert "reference sphere" in run(
        *args, "--output", tmp_path / "wrong", "--body", "Moon", "--body-radius", 1737400, ok=False
    )
    assert "encoding range" in run(
        *args, "--output", tmp_path / "range", "--input-offset", -10101, ok=False
    )


def jpeg(path, color):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.empty((3, 256, 256), np.uint8)
    data[:] = np.array(color)[:, None, None]
    with rasterio.open(
        path, "w", driver="JPEG", width=256, height=256, count=3, dtype="uint8", quality=90
    ) as target:
        target.write(data)


def test_cloudless_stitch_resume_and_earth_cube(tmp_path):
    listed = load(ROOT / "datasets/s2cloudless-2020.json")
    assert listed["body"] == "Earth" and listed["radiusMeters"] == 6378137
    assert "32768 requests" in run("cloudless", "--info", "--output", tmp_path / "info.tif")
    assert not (tmp_path / "info.tif").exists()
    root = tmp_path / "tiles"
    jpeg(root / "0/0/0.jpg", [10, 20, 30])
    jpeg(root / "0/0/1.jpg", [200, 180, 160])
    output = tmp_path / "mosaic.tif"
    args = [
        "cloudless",
        "--output",
        output,
        "--zoom",
        0,
        "--url-template",
        root.as_uri() + "/{matrix}/{row}/{col}.jpg",
    ]
    assert "2 x 1 tiles" in run(*args, "--jobs", 2)
    image = pixels(output)
    assert image.shape == (256, 512, 3)
    assert np.all(np.abs(image[0, 0].astype(int) - [10, 20, 30]) <= 2)
    assert np.all(np.abs(image[128, 400].astype(int) - [200, 180, 160]) <= 2)
    cache = tmp_path / "mosaic-tiles/0/0/0.jpg"
    stamp, cache_stamp = output.stat().st_mtime_ns, cache.stat().st_mtime_ns
    assert "no download needed" in run(*args)
    assert output.stat().st_mtime_ns == stamp
    assert "another download" in run(*args, "--zoom", 1, ok=False)
    output.unlink()
    Path(str(output) + ".receipt.json").unlink()
    run(*args)
    assert cache.stat().st_mtime_ns == cache_stamp
    config = tmp_path / "earth.json"
    save(config, {**listed, "sourceFile": str(output)})
    manifest = build(output, tmp_path / "pyramid", "--config", config, "--max-level", 0)
    assert manifest["body"] == "Earth" and "CC BY-NC-SA 4.0" in manifest["source"]["credit"]
    assert np.all(pixels(tmp_path / "pyramid/0/0/0/0.png")[:, :, 3] == 255)
    for options in [
        ("--zoom", 8),
        ("--jobs", 0),
        ("--jobs", 33),
        ("--url-template", "https://example.test/tile.jpg"),
    ]:
        run("cloudless", "--output", tmp_path / "rejected/m.tif", *options, ok=False)
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
    run(
        "cloudless",
        "--output",
        output,
        "--zoom",
        0,
        "--url-template",
        root.as_uri() + "/{matrix}/{row}/{col}.jpg",
    )
    assert list(pixels(output)[128, 400]) == [255, 255, 255]


def test_earth_elevation_trenches_nodata_and_tiles(tmp_path):
    info = run("earth-elevation", "--info", "--output", tmp_path / "info")
    assert "465,969,062 bytes" in info and "16384 x 8192" in info
    assert not (tmp_path / "info").exists()
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
        run("earth-elevation", "--source", path, "--output", grid, "--width", 8, "--height", 4)
        manifest = load(grid / "manifest.json")
        assert manifest["body"] == "Earth" and manifest["radiusMeters"] == 6378137
        assert manifest["minMeters"] == manifest["maxMeters"] == expected
    output = tmp_path / "tiles"
    run(
        "height-tiles",
        "--source",
        tmp_path / "trench/manifest.json",
        "--output",
        output,
        "--tile-size",
        16,
        "--max-level",
        0,
    )
    tiled = load(output / "manifest.json")
    assert tiled["minMeters"] == tiled["maxMeters"] == -10000
    assert all(info["min"] == info["max"] == -10000 for info in tiled["tiles"].values())
    source = fixture(
        tmp_path / "sphere.tif", "--body-radius", 6378137, "--height", "--constant", 100
    )
    assert "geographic" in run(
        "earth-elevation",
        "--source",
        source,
        "--output",
        tmp_path / "wrong",
        "--width",
        8,
        "--height",
        4,
        ok=False,
    )


@pytest.mark.parametrize(
    "name,size,filename",
    [
        ("mars-mola-463m.json", 2125771142, "Mars_MGS_MOLA_DEM_mosaic_global_463m.tif"),
        (
            "mars-viking-mdim21-232m.json",
            12742411029,
            "Mars_Viking_MDIM21_ClrMosaic_global_232m.tif",
        ),
    ],
)
def test_mars_catalog_integrity(name, size, filename):
    config = ROOT / "datasets" / name
    listed = load(config)
    assert listed["body"] == "Mars" and listed["radiusMeters"] == 3396190
    assert listed["expectedBytes"] == size
    info = run("fetch", "--config", config, "--info")
    assert f"{size:,} bytes" in info and filename in info


def test_earth_sites_keep_geodetic_angles():
    assert [s["id"] for s in load(ROOT / "datasets/earth-sites.json")["sites"]] == [
        "starbase",
        "ksc",
    ]
    info = run("earth-sites", "--site", "ksc", "--info")
    assert "Kennedy Space Center" in info and "LC-39A" in info
    assert re.search(r"levels 8\.\.16; \d+ tiles", info)
    shift = run("earth-sites", "--self-test")
    assert float(re.search(r"geodetic shift (\d+\.\d+) m", shift)[1]) < 1.5


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
    run("earth-elevation", "--source", path, "--output", grid, "--width", 8, "--height", 4)
    encoded = np.frombuffer((grid / "heights.u16").read_bytes(), dtype="<u2").reshape(4, 8)
    assert len(set(encoded[0].tolist())) == 1
    assert len(set(encoded[-1].tolist())) == 1
    assert encoded[0, 0] != encoded[-1, 0]


def test_elevation_keeps_a_mismatched_source(tmp_path):
    source = tmp_path / "ldem.tif"
    source.write_bytes(b"not a dem")
    output = tmp_path / "out"
    message = run("elevation", "--source", source, "--output", output, ok=False)
    assert "checksum" in message
    assert source.read_bytes() == b"not a dem"
    assert not output.exists()


def test_face_uv_grid_matches_scalar():
    from pipeline.heights import _face_uv, _face_uv_grid

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
    faces, us, vs = _face_uv_grid(vectors.T.reshape(3, 2, 4))
    for index, vector in enumerate(vectors):
        face, u, v = _face_uv(vector)
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
    run(
        "height-tiles",
        "--source",
        config,
        "--output",
        base,
        "--tile-size",
        16,
        "--max-level",
        1,
    )
    paged = tmp_path / "paged"
    shutil.copytree(base, paged)
    manifest = load(paged / "manifest.json")
    kept, grouped = {}, {}
    for key, info in manifest["tiles"].items():
        face, level, tx, ty = (int(part) for part in key.split("/"))
        if level == 0:
            kept[key] = info
        else:
            grouped.setdefault(f"{face}/0/0/0", {})[key] = info
    pages = {}
    for parent, entries in grouped.items():
        relative = f"metadata/{parent}.json"
        save(paged / relative, {"tiles": entries})
        pages[parent] = {
            "path": relative,
            "sha256": hashlib.sha256((paged / relative).read_bytes()).hexdigest(),
            "lower": kept[parent]["lower"],
            "upper": kept[parent]["upper"],
        }
    manifest["tiles"] = kept
    manifest["pages"] = pages
    manifest["pageLevel"] = 0
    save(paged / "manifest.json", manifest)
    fine = fixture(tmp_path / "fine.tif", "--height", "--constant", 40)
    region = [
        "height-region",
        "--source",
        fine,
        "--id",
        "patch",
        "--latitude",
        "0",
        "--longitude",
        "0",
        "--radius-meters",
        "500",
        "--max-level",
        "2",
    ]
    run(*region, "--output", base)
    run(*region, "--output", paged)

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


def test_etopo_checksum_and_receipt_precede_rename(tmp_path, monkeypatch):
    from pipeline import sources

    official = tmp_path / "ETOPO.tif"
    body = b"II*\x00" + b"x" * 12
    bad = b"II*\x00" + b"y" * 12
    digest = hashlib.sha256(body).hexdigest()
    partial = Path(str(official) + ".part")
    receipt = Path(str(official) + ".receipt.json")

    def refuse_download(*_args, **_kwargs):
        raise AssertionError("downloaded")

    monkeypatch.setattr(sources, "ETOPO_SOURCE", str(official))
    monkeypatch.setattr(sources, "ETOPO_BYTES", len(body))
    monkeypatch.setattr(sources, "ETOPO_SHA", digest)
    monkeypatch.setattr(sources, "download", refuse_download)
    partial.write_bytes(bad)
    with pytest.raises(RuntimeError, match="checksum"):
        sources._ensure_etopo(official)
    assert partial.read_bytes() == bad and not official.exists() and not receipt.exists()
    partial.write_bytes(body)
    real_replace = os.replace

    def fail_source_rename(src, dst):
        if str(src).endswith(".part"):
            raise OSError("rename failed")
        real_replace(src, dst)

    monkeypatch.setattr(sources.os, "replace", fail_source_rename)
    with pytest.raises(OSError, match="rename failed"):
        sources._ensure_etopo(official)
    assert load(receipt)["sha256"] == digest and not official.exists() and partial.exists()
    monkeypatch.setattr(sources.os, "replace", real_replace)
    sources._ensure_etopo(official)
    assert official.read_bytes() == body and not partial.exists()
    receipt.write_text(receipt.read_text().replace(digest, "0" * 64))
    with pytest.raises(RuntimeError, match="receipt"):
        sources._ensure_etopo(official)
    assert official.read_bytes() == body


def test_second_interrupt_uses_the_default_handler(monkeypatch):
    from pipeline import runtime

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
