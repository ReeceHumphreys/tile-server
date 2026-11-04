"""Numeric elevation tiles: a global uint16 cube pyramid plus regional refinements.

Both builders write through `HeightIndex`, which owns the manifest and page
format. Regional sources blend into a frozen snapshot of the global base.
"""

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from tile_service.cube import (
    HEIGHT_OFFSET,
    HEIGHT_SCALE,
    Coverage,
    Tile,
    encode_heights,
    face_crs,
    pyramid,
    tile_affine,
    tile_directions,
)
from tile_service.height_index import HeightIndex, bilinear, read_values
from tile_service.records import BuildRecord
from tile_service.runtime import check, log, read_json, require_sphere, sha256

REGION_ID = set("abcdefghijklmnopqrstuvwxyz0123456789-")


def build(grid_path, out, *, tile_size=128, gutter=2, max_level=4):
    """Build (or resume) the global height pyramid from a prepared grid manifest."""
    grid_path, out = Path(grid_path), Path(out)
    grid = read_json(grid_path)
    width = int(grid["width"])
    height = int(grid["height"])
    size, level = tile_size, max_level
    radius = float(grid.get("radiusMeters") or 0)
    if not (
        grid.get("format") == "uint16-le"
        and grid.get("scaleMeters") == HEIGHT_SCALE
        and grid.get("offsetMeters") == HEIGHT_OFFSET
        and math.isfinite(radius)
        and radius > 0
        and grid.get("body")
        and width > 1
        and height > 1
    ):
        raise RuntimeError("Expected prepared numeric elevation grid")
    if not (16 <= size <= 256 and 2 <= gutter <= 8 and 0 <= level <= 5):
        raise RuntimeError("Invalid height grid settings (max level 5)")
    raw = grid_path.parent / grid["data"]
    digest = sha256(raw)
    if digest != grid.get("dataSha256"):
        raise RuntimeError("Global elevation checksum mismatch")
    identity = {
        "sourceSha256": digest,
        "tileSize": size,
        "gutter": gutter,
        "maxLevel": level,
        "pipelineVersion": 1,
    }
    if grid["body"] != "Moon":
        identity["body"] = grid["body"]
        identity["bodyRadius"] = radius
    BuildRecord(out, identity)
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        # Written last; regional refinements may have been added since.
        log(f"Up to date: {out}")
        return read_json(manifest_path)
    values = read_values(raw, width * height)
    index = HeightIndex.create(
        out,
        body=grid["body"],
        radius=grid["radiusMeters"],
        tile_size=size,
        gutter=gutter,
        max_level=level,
    )
    prefix = "lola" if grid["body"] == "Moon" else "global"
    completed = 0
    for face in range(6):
        for depth, x, y in pyramid(level):
            check()
            tile = Tile(face, depth, x, y)
            path = f"{prefix}/{tile.key}.u16"
            if (out / path).exists():
                encoded = index.read(path)
            else:
                _, _, vectors = tile_directions(tile, size, gutter)
                sample_u = (np.atan2(-vectors[2], vectors[0]) + math.pi) / (2 * math.pi)
                sample_v = np.atan2(np.hypot(vectors[0], vectors[2]), vectors[1]) / math.pi
                meters = bilinear(
                    values,
                    width,
                    height,
                    sample_u * width - 0.5,
                    sample_v * height - 0.5,
                    wrap=True,
                )
                encoded = encode_heights(meters)
            index.put(tile.key, path, encoded)
            completed += 1
            if completed % 200 == 0:
                log(f"{completed} height tiles ready")
    index.add_source(
        {"id": prefix, "credit": grid.get("credit"), "sourcePage": grid.get("sourcePage")}
    )
    index.publish(level)
    log(f"Published {manifest_path} ({completed} numeric height tiles)")
    return index.document


def add_region(
    root,
    source,
    region_id,
    latitude,
    longitude,
    extent=2000,
    *,
    max_level=14,
    input_scale=1,
    input_offset=0,
    credit="User-supplied DEM",
    source_page=None,
    overview=None,
    feather=None,
):
    """Blend a regional DEM into the height pyramid at `root` (resumable)."""
    root, source = Path(root), Path(source)
    if not region_id or region_id in ("lola", "global") or not set(region_id) <= REGION_ID:
        raise RuntimeError("Invalid height source ID")
    index = HeightIndex.open(root)
    body_radius = index.radius
    if not math.isfinite(body_radius) or body_radius <= 0:
        raise RuntimeError("Invalid body radius")
    size, gutter, base_max = index.tile_size, index.gutter, index.max_level
    scale, offset = float(input_scale), float(input_offset)
    if not (math.isfinite(scale) and scale > 0 and math.isfinite(offset) and 0 < max_level <= 16):
        raise RuntimeError("Invalid height region settings")
    with rasterio.open(source) as src:
        if src.count != 1:
            raise RuntimeError("Height source must have one numeric band")
        require_sphere(
            src.crs,
            body_radius,
            "Height source must be georeferenced to the configured body sphere",
        )
        identity = {"pipelineVersion": 1}
        settings = {}
        if overview is not None:
            settings["overview"] = overview
        if feather is not None:
            settings["featherMeters"] = feather
        if settings:
            identity["coverage"] = settings
        identity.update(
            {
                "sourceSha256": sha256(source),
                # Keep `deg * pi / 180`: math.radians differs in the last bit,
                # which would orphan resumable builds recorded by older versions.
                "latitude": latitude * math.pi / 180,
                "longitude": longitude * math.pi / 180,
                "radius": float(extent),
                "maxLevel": max_level,
                "inputScale": scale,
                "inputOffset": offset,
            }
        )
        if index.body != "Moon":
            identity["body"] = index.body
            identity["bodyRadius"] = body_radius
        work = root / "sources" / region_id
        BuildRecord(work, identity, state="state.json")
        if index.has_source(region_id):
            # Listed sources are complete: the manifest is published last.
            log(f"Up to date: height source {region_id}")
            return index.document
        base = HeightIndex.frozen(root, work, index.document)
        base_level = base.complete_level()
        if max_level <= base_level:
            raise RuntimeError("Height region must refine beyond the complete global pyramid")
        coverage = Coverage(
            latitude,
            longitude,
            float(extent),
            body_radius,
            base_level,
            max_level,
            overview=overview,
            feather=feather,
        )
        addresses = coverage.tiles
        index.load(addresses)
        tile_width = size + 2 * gutter
        completed = 0
        for address in addresses:
            check()
            path = f"{region_id}/{address.key}.u16"
            if (root / path).exists():
                values = index.read(path)
            else:
                sampled = np.zeros((2, tile_width, tile_width), np.float32)
                reproject(
                    source=rasterio.band(src, [1]),
                    destination=sampled,
                    dst_transform=tile_affine(
                        address.face, address.level, address.x, address.y, size, gutter, body_radius
                    ),
                    dst_crs=face_crs(address.face, body_radius),
                    resampling=Resampling.bilinear,
                    dst_alpha=2,
                    num_threads=1,
                    warp_mem_limit=128,
                )
                _, _, vectors = tile_directions(address, size, gutter)
                coarse = base.sample(vectors)
                distance = coverage.distance(vectors, body_radius)
                fine = sampled[0] * scale + offset
                usable = (sampled[1] > 0) & np.isfinite(sampled[0])
                mix = np.where(usable, coverage.weight(distance), 0)
                values = encode_heights(np.where(mix > 0, coarse * (1 - mix) + fine * mix, coarse))
            index.put(address.key, path, values)
            completed += 1
            if completed % 200 == 0:
                log(f"{completed}/{len(addresses)} regional height tiles ready")
    index.document["maxLevel"] = max(base_max, max_level)
    if not index.has_source(region_id):
        entry = {
            "id": region_id,
            "credit": credit,
            "sourcePage": source_page,
            "sha256": identity["sourceSha256"],
            "featherMeters": coverage.feather,
            "inputScale": scale,
            "inputOffset": offset,
        }
        if coverage.description is not None:
            entry["overview"] = coverage.description
        index.add_source(entry)
    index.publish(base_level)
    log(f"Published regional height source {region_id}")
    return index.document
