"""This tool against the contract's conformance files, the same ones any other tool uses."""

import json
from pathlib import Path

import numpy as np
import pytest

from tile_service import documents
from tile_service.roots import (
    Tile,
    decode_heights,
    direction,
    lonlat_vector,
    root_max_level,
    tile_directions,
    uv_on,
)

CONFORMANCE = Path(__file__).resolve().parents[1] / "spec/conformance"
GRID = json.loads((CONFORMANCE / "grid.json").read_text())
CASES = json.loads((CONFORMANCE / "documents.json").read_text())
TOLERANCE = 1e-9


@pytest.mark.parametrize("point", GRID["points"], ids=lambda p: f"{p['root']}-{p['u']}-{p['v']}")
def test_positions_match_the_contract(point):
    expected = lonlat_vector(point["latitude"], point["longitude"])
    actual = direction(point["root"], point["u"], point["v"])
    assert np.allclose(actual, expected, atol=TOLERANCE)
    edge = point["root"] < 2 and (point["u"] in (0, 1) or point["v"] in (0, 1))
    if not edge:
        u, v = uv_on(point["root"], expected)
        assert (u, v) == pytest.approx((point["u"], point["v"]), abs=TOLERANCE)


def test_levels_match_the_contract():
    for case in GRID["levels"]:
        assert root_max_level(case["root"], case["maxLevel"]) == case["rootMaxLevel"]


def test_pixels_match_the_contract():
    for case in GRID["pixels"]:
        tile = Tile(case["root"], case["level"], case["x"], case["y"])
        _, _, vectors = tile_directions(tile, case["tileSize"], case["gutter"])
        actual = vectors[:, case["row"], case["col"]]
        expected = direction(case["root"], case["u"], case["v"])
        assert np.allclose(actual, expected, atol=TOLERANCE)


def test_heights_match_the_contract():
    for case in GRID["heights"]:
        meters = decode_heights(case["stored"], case["scaleMeters"], case["offsetMeters"])
        assert meters == pytest.approx(case["meters"], abs=TOLERANCE)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_documents_match_the_contract(case):
    def check():
        if case["type"] == "regions":
            documents.validate_regions(case["document"], case["base"])
        elif case["type"] == "heights":
            documents.validate_heights(case["document"])
        else:
            documents.validate_imagery(case["document"])

    if case["valid"]:
        check()
    else:
        with pytest.raises(documents.InvalidDocument):
            check()
