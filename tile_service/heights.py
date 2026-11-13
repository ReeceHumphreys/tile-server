"""Numeric elevation tiles: a global uint16 pyramid plus regional refinements.

Both builders write through `HeightIndex`, which owns the manifest and page
format. Regional sources blend into a frozen snapshot of the global base.
"""

import math
import os
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from tile_service.documents import SITE_ID
from tile_service.height_index import HeightIndex, read_values
from tile_service.records import BuildRecord
from tile_service.roots import (
    ROOTS,
    Coverage,
    Tile,
    bilinear,
    decode_heights,
    east_west_pixels,
    encode_heights,
    equirect_crs,
    pyramid,
    require_warpable,
    root_crs,
    root_max_level,
    smooth_row,
    tile_affine,
    tile_directions,
)
from tile_service.runtime import (
    atomic_write,
    check,
    log,
    radii,
    read_json,
    require_sphere,
    run_tiles,
    sha256,
)


def _onto_sphere(source, radius, latitude, longitude, extent):
    """`source`, or if it lies on the body's own flattened ellipsoid, a copy warped onto the
    body's sphere over the site. Geodetic degrees go onto the sphere as degrees, as NAIP
    imagery does, so a DEM in UTM on GRS80 or WGS84 lands where Earth's tiles expect it."""
    source = Path(source)
    with rasterio.open(source) as src:
        if src.crs is None or src.count != 1:
            return source
        major, minor = radii(src.crs)
        if abs(major - radius) >= 0.01 or abs(minor - major) < 0.01:
            return source
        pixel = min(map(abs, src.res))
        if src.crs.is_geographic:
            pixel = math.radians(pixel) * radius
        reach = extent + 2 * pixel
        lat, lon = math.radians(latitude), math.radians(longitude)
        north, west = lat + reach / radius, lon - reach / (radius * math.cos(lat))
        width = math.ceil(2 * (lon - west) * radius / pixel)
        height = math.ceil(2 * (north - lat) * radius / pixel)
        transform = rasterio.transform.Affine(pixel, 0, west * radius, 0, -pixel, north * radius)
        nodata = src.nodata if src.nodata is not None else -9999.0
        values = np.full((height, width), nodata, np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=values,
            dst_transform=transform,
            dst_crs=equirect_crs(radius),
            src_nodata=src.nodata,
            dst_nodata=nodata,
            resampling=Resampling.bilinear,
        )
    output = source.with_name(source.stem + ".sphere.tif")
    partial = Path(str(output) + ".partial")
    with rasterio.open(
        partial,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="float32",
        crs=equirect_crs(radius),
        transform=transform,
        nodata=nodata,
        compress="deflate",
        tiled=True,
    ) as dataset:
        dataset.write(values, 1)
    os.replace(partial, output)
    log(f"Warped {source.name} onto the {radius:.0f} m sphere")
    return output


def _square_pixels(values, width, height):
    """Smooth a grid along longitude so its samples are about square on the ground."""
    grid = values.reshape(height, width).copy()
    for row in range(height):
        count = east_west_pixels(90 - (row + 0.5) * 180 / height, width)
        if count >= 3:
            grid[row] = smooth_row(grid[row], count, "wrap")
    return grid.ravel()


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
        and grid.get("scaleMeters") > 0
        and math.isfinite(grid.get("offsetMeters"))
        and math.isfinite(radius)
        and radius > 0
        and grid.get("body")
        and width > 1
        and height > 1
    ):
        raise RuntimeError("Expected prepared numeric elevation grid")
    if not (16 <= size <= 256 and 2 <= gutter <= 8 and 0 <= level <= 6):
        raise RuntimeError("Invalid height grid settings (max level 6)")
    raw = grid_path.parent / grid["data"]
    digest = sha256(raw)
    if digest != grid.get("dataSha256"):
        raise RuntimeError("Global elevation checksum mismatch")
    identity = {
        "sourceSha256": digest,
        "tileSize": size,
        "gutter": gutter,
        "maxLevel": level,
        "pipelineVersion": 3,
        "body": grid["body"],
        "bodyRadius": radius,
    }
    BuildRecord(out, identity)
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        # Written last; regional refinements may have been added since.
        log(f"Up to date: {out}")
        return read_json(manifest_path)
    values = _square_pixels(read_values(raw, width * height), width, height)
    index = HeightIndex.create(
        out,
        body=grid["body"],
        radius=grid["radiusMeters"],
        tile_size=size,
        gutter=gutter,
        max_level=level,
        scale=grid["scaleMeters"],
        offset=grid["offsetMeters"],
        vertical_datum=grid["verticalDatum"],
    )
    encoding = index.encoding
    prefix = "global"
    completed = 0
    for root in ROOTS:
        for depth, x, y in pyramid(root_max_level(root, level)):
            check()
            tile = Tile(root, depth, x, y)
            path = f"{prefix}/{tile.key}.u16"
            if (out / path).exists():
                encoded = index.read(path)
            else:
                _, _, vectors = tile_directions(tile, size, gutter)
                sample_u = (np.atan2(-vectors[2], vectors[0]) + math.pi) / (2 * math.pi)
                sample_v = np.atan2(np.hypot(vectors[0], vectors[2]), vectors[1]) / math.pi
                stored = bilinear(
                    values.reshape(height, width),
                    sample_u * width - 0.5,
                    sample_v * height - 0.5,
                    wrap=True,
                )
                encoded = encode_heights(decode_heights(stored, *encoding), *encoding)
            index.put(tile.key, path, encoded)
            completed += 1
            if completed % 200 == 0:
                log(f"{completed} height tiles ready")
    index.add_source(
        {"id": prefix, "credit": grid.get("credit", ""), "sourcePage": grid.get("sourcePage", "")}
    )
    index.publish(level)
    log(f"Published {manifest_path} ({completed} numeric height tiles)")
    return index.document


def _write_batch(addresses, source, root, work, region_id, coverage, scale, offset):
    """Write regional tiles, blended over the frozen base that `work` holds."""
    base = HeightIndex.frozen(root, work, None)
    size, gutter, radius = base.tile_size, base.gutter, base.radius
    width = size + 2 * gutter
    with rasterio.open(source) as src:
        for address in addresses:
            check()
            sampled = np.zeros((2, width, width), np.float32)
            reproject(
                source=rasterio.band(src, [1]),
                destination=sampled,
                dst_transform=tile_affine(
                    address.root, address.level, address.x, address.y, size, gutter, radius
                ),
                dst_crs=root_crs(address.root, radius),
                resampling=Resampling.bilinear,
                dst_alpha=2,
                num_threads=1,
                warp_mem_limit=128,
            )
            _, _, vectors = tile_directions(address, size, gutter)
            coarse = base.sample(vectors)
            distance = coverage.distance(vectors, radius)
            fine = sampled[0] * scale + offset
            usable = (sampled[1] > 0) & np.isfinite(sampled[0])
            mix = np.where(usable, coverage.weight(distance), 0)
            values = encode_heights(
                np.where(mix > 0, coarse * (1 - mix) + fine * mix, coarse), *base.encoding
            )
            atomic_write(
                root / f"{region_id}/{address.key}.u16",
                np.ascontiguousarray(values.astype("<u2")).tobytes(),
            )


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
    source_page="",
    overview=None,
    feather=None,
):
    """Blend a regional DEM into the height pyramid at `root` (resumable)."""
    root, source = Path(root), Path(source)
    if region_id == "global" or not SITE_ID.fullmatch(region_id):
        raise RuntimeError("Invalid height source ID")
    index = HeightIndex.open(root)
    body_radius = index.radius
    if not math.isfinite(body_radius) or body_radius <= 0:
        raise RuntimeError("Invalid body radius")
    base_max = index.max_level
    scale, offset = float(input_scale), float(input_offset)
    if not (math.isfinite(scale) and scale > 0 and math.isfinite(offset) and 0 < max_level <= 17):
        raise RuntimeError("Invalid height region settings")
    source_sha256 = sha256(source)
    source = _onto_sphere(source, body_radius, latitude, longitude, float(extent))
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
                "sourceSha256": source_sha256,
                "latitude": latitude,
                "longitude": longitude,
                "radius": float(extent),
                "maxLevel": max_level,
                "inputScale": scale,
                "inputOffset": offset,
                "body": index.body,
                "bodyRadius": body_radius,
            }
        )
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
        require_warpable(src, coverage.root, body_radius)
        addresses = coverage.tiles
        index.load(addresses)
    missing = [a for a in addresses if not (root / f"{region_id}/{a.key}.u16").exists()]
    run_tiles(_write_batch, missing, source, root, work, region_id, coverage, scale, offset)
    # Recording stays in this process: the index is one file, and tiles arrive in any order.
    for completed, address in enumerate(addresses, start=1):
        check()
        path = f"{region_id}/{address.key}.u16"
        index.put(address.key, path, index.read(path))
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
