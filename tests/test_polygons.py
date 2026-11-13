import json

import rasterio

from tile_service import imagery, polygons
from tile_service.roots import EARTH_RADIUS

DATASET = {
    "id": "square",
    "body": "Earth",
    "title": "A square",
    "radiusMeters": EARTH_RADIUS,
    "tileSize": 16,
    "gutter": 1,
    "maxLevel": 1,
}
# Rows and columns are 5.625 degrees, so this edge runs through the middle of a pixel.
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [0, 45], [47.8125, 45], [47.8125, 0], [0, 0]]],
}


def drawn(tmp_path):
    geojson = tmp_path / "land.geojson"
    geojson.write_text(json.dumps({"features": [{"geometry": SQUARE}]}))
    return polygons.draw(
        geojson, tmp_path / "land.tif", radius=EARTH_RADIUS, width=64, fill=200, background=20
    )


def test_polygons_fill_their_ground_with_soft_edges(tmp_path):
    with rasterio.open(drawn(tmp_path)) as src:
        pixels = src.read(1)
    # Column 32 is longitude 0 and row 16 is latitude 0.
    assert pixels[10, 35] == 200
    assert pixels[10, 20] == 20
    assert 20 < pixels[10, 40] < 200


def test_a_drawn_map_builds_as_imagery(tmp_path):
    manifest = imagery.build(drawn(tmp_path), tmp_path / "out", DATASET, jobs=1)
    assert manifest["source"]["bands"] == 1
    assert (tmp_path / "out" / "0" / "1" / "0" / "0.png").exists()
