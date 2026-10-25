"""Run against Caddy itself: TILE_TEST_URL=http://127.0.0.1:8787 uv run pytest -m deployment."""

import json
import os
import urllib.error
import urllib.request

import pytest

URL = os.environ.get("TILE_TEST_URL", "").rstrip("/")
pytestmark = [pytest.mark.deployment, pytest.mark.skipif(not URL, reason="TILE_TEST_URL not set")]


def request(path, method="GET", headers=None):
    req = urllib.request.Request(URL + path, method=method, headers=headers or {})
    try:
        return urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as error:
        return error


def test_real_static_server_ranges_cors_and_conditional_requests():
    assert request("/healthz").status == 200
    release = json.load(request("/release.json"))
    document = release["documents"][0]
    manifest = json.load(request("/data/" + document))
    if "storage" not in manifest:
        pytest.fail("First smoke-test document must be a packed tile manifest")
    archive = next(iter(manifest["storage"]["archives"].values()))
    path = "/data/" + document.rsplit("/", 1)[0] + "/" + archive
    head = request(path, "HEAD")
    assert head.status == 200 and head.read() == b""
    assert head.headers["Content-Type"] == "application/vnd.pmtiles"
    assert "immutable" in head.headers["Cache-Control"]
    assert head.headers["Accept-Ranges"] == "bytes"
    assert int(head.headers["Content-Length"]) > 127
    etag = head.headers["ETag"]
    part = request(path, headers={"Range": "bytes=0-6", "If-Match": etag})
    assert part.status == 206 and part.read() == b"PMTiles"
    assert part.headers["Content-Range"].startswith("bytes 0-6/")
    cross_origin = request(path, headers={"Range": "bytes=0-6", "Origin": "https://example.test"})
    assert cross_origin.status == 206
    assert cross_origin.headers.get_all("Access-Control-Allow-Origin") == ["*"]
    assert request(path, headers={"If-Match": '"wrong"'}).status == 412
    assert request(path, headers={"If-None-Match": etag}).status == 304
    assert request(path, headers={"Range": "bytes=999999999999-"}).status == 416
    options = request(
        path,
        "OPTIONS",
        {"Origin": "https://example.test", "Access-Control-Request-Headers": "range,if-match"},
    )
    assert options.status == 204 and options.headers["Access-Control-Allow-Origin"] == "*"
    assert "If-Match" in options.headers["Access-Control-Allow-Headers"]
    assert "ETag" in options.headers["Access-Control-Expose-Headers"]
    assert request(path, "POST").status == 405
    missing = request("/data/missing.pmtiles")
    assert missing.status == 404 and not missing.read().lstrip().startswith(b"<!DOCTYPE")
    assert "immutable" not in request("/data/" + document).headers["Cache-Control"]
