import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from pipeline.region import Address, Coverage
from pipeline.runtime import (
    PI,
    Args,
    atomic_json,
    atomic_write,
    check,
    direction,
    face_crs,
    log,
    read_json,
    require_sphere,
    sha256,
    tile_affine,
)


def _read_values(path, count):
    data = Path(path).read_bytes()
    if len(data) != count * 2:
        raise RuntimeError("Incorrect height tile byte count")
    return np.frombuffer(data, dtype="<u2").copy()


def _grid(values, width, height, xs, ys, wrap=False):
    values = np.asarray(values).reshape(height, width)
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if not wrap:
        xs = np.clip(xs, 0, width - 1)
    ys = np.clip(ys, 0, height - 1)
    ix = np.floor(xs).astype(np.int64)
    iy = np.floor(ys).astype(np.int64)
    fx = xs - ix
    fy = ys - iy

    def at(xx, yy):
        if wrap:
            xx = np.mod(xx, width)
        else:
            xx = np.clip(xx, 0, width - 1)
        yy = np.clip(yy, 0, height - 1)
        return values[yy, xx]

    return (
        (1 - fy) * ((1 - fx) * at(ix, iy) + fx * at(ix + 1, iy))
        + fy * ((1 - fx) * at(ix, iy + 1) + fx * at(ix + 1, iy + 1))
    ) * 0.5 - 10000


def _encode(meters):
    meters = np.asarray(meters, dtype=float)
    if meters.size and (
        not np.isfinite(meters).all()
        or float(meters.min()) < -10000
        or float(meters.max()) > 22767.5
    ):
        raise RuntimeError("Height out of encoding range; check source units/datum/nodata")
    return np.rint((meters + 10000) * 2).astype(np.uint16)


def _write_height(root, path, values):
    target = root / path
    if not target.exists():
        atomic_write(target, np.ascontiguousarray(values.astype("<u2")).tobytes())
    low = float(values.min()) * 0.5 - 10000
    high = float(values.max()) * 0.5 - 10000
    return {
        "path": path,
        "min": low,
        "max": high,
        "lower": low,
        "upper": high,
        "sha256": sha256(target),
    }


def _propagate(manifest):
    tiles = manifest["tiles"]
    for level in range(int(manifest["maxLevel"]), 0, -1):
        for key, child in list(tiles.items()):
            face, depth, x, y = (int(part) for part in key.split("/"))
            if depth != level:
                continue
            parent_key = f"{face}/{depth - 1}/{x >> 1}/{y >> 1}"
            parent = tiles.get(parent_key)
            if parent is None:
                raise RuntimeError("Height hierarchy missing parent")
            parent["lower"] = min(parent["lower"], child["lower"])
            parent["upper"] = max(parent["upper"], child["upper"])
    roots = [tiles[f"{face}/0/0/0"] for face in range(6)]
    manifest["minMeters"] = min(root["lower"] for root in roots)
    manifest["maxMeters"] = max(root["upper"] for root in roots)


def _publish(root, manifest, page_level):
    # Generated files have changed; old archive indexes must not remain active.
    # The publication step (`data:pack`) creates references for this new content.
    manifest.pop("storage", None)
    tiles = manifest["tiles"]
    pages = manifest.get("pages") or {}
    groups = {}
    remove = []
    for key, info in list(tiles.items()):
        face, level, x, y = (int(part) for part in key.split("/"))
        if level <= page_level:
            continue
        shift = level - page_level
        parent = f"{face}/{page_level}/{x >> shift}/{y >> shift}"
        groups.setdefault(parent, {})[key] = info
        remove.append(key)
    for parent, entries in groups.items():
        path = f"metadata/{parent}.json"
        atomic_json(root / path, {"tiles": entries})
        pages[parent] = {
            "path": path,
            "sha256": sha256(root / path),
            "lower": tiles[parent]["lower"],
            "upper": tiles[parent]["upper"],
        }
    for key in remove:
        del tiles[key]
    manifest["pages"] = pages
    manifest["pageLevel"] = page_level
    atomic_json(root / "manifest.json", manifest)


def _empty_manifest(size, gutter, level, source):
    return {
        "version": 1,
        "body": source["body"],
        "radiusMeters": source["radiusMeters"],
        "projection": "normalized-cube",
        "format": "uint16-le",
        "scaleMeters": 0.5,
        "offsetMeters": -10000,
        "sources": [],
        "tileSize": size,
        "gutter": gutter,
        "maxLevel": level,
        "tiles": {},
    }


def _compatible(state, identity):
    if state.exists():
        if read_json(state) != identity:
            raise RuntimeError("Height output belongs to another source/settings")
    else:
        atomic_json(state, identity)


def _face_uv(vector):
    axis = int(np.argmax(np.abs(vector)))
    positive = vector[axis] >= 0
    if axis == 0:
        face, u, v, w = (
            (0, -vector[2], vector[1], vector[0])
            if positive
            else (1, vector[2], vector[1], -vector[0])
        )
    elif axis == 1:
        face, u, v, w = (
            (2, vector[0], -vector[2], vector[1])
            if positive
            else (3, vector[0], vector[2], -vector[1])
        )
    else:
        face, u, v, w = (
            (4, vector[0], vector[1], vector[2])
            if positive
            else (5, -vector[0], vector[1], -vector[2])
        )
    return face, (1 + u / w) / 2, (1 + v / w) / 2


def _face_uv_grid(vectors):
    """Same face pick as `_face_uv`, for a whole tile of direction vectors."""
    x, y, z = vectors
    axis = np.argmax(np.abs(vectors), axis=0)
    positive = np.take_along_axis(vectors, axis[None, ...], axis=0)[0] >= 0
    face = np.empty(x.shape, np.int64)
    u = np.empty(x.shape, np.float64)
    v = np.empty(x.shape, np.float64)
    w = np.empty(x.shape, np.float64)
    for mask, face_id, uu, vv, ww in (
        ((axis == 0) & positive, 0, -z, y, x),
        ((axis == 0) & ~positive, 1, z, y, -x),
        ((axis == 1) & positive, 2, x, -z, y),
        ((axis == 1) & ~positive, 3, x, z, -y),
        ((axis == 2) & positive, 4, x, y, z),
        ((axis == 2) & ~positive, 5, -x, y, -z),
    ):
        face[mask] = face_id
        u[mask] = uu[mask]
        v[mask] = vv[mask]
        w[mask] = ww[mask]
    return face, (1 + u / w) / 2, (1 + v / w) / 2


def height_tiles(args: Args):
    args.validate({"--source", "--output", "--tile-size", "--gutter", "--max-level"})
    source = Path(args.get("--source", "public/data/moon/elevation/manifest.json"))
    out = Path(args.get("--output", "public/data/moon/elevation-tiles"))
    grid = read_json(source)
    width = int(grid["width"])
    height = int(grid["height"])
    size = args.integer("--tile-size", 128)
    gutter = args.integer("--gutter", 2)
    level = args.integer("--max-level", 4)
    radius = float(grid.get("radiusMeters") or 0)
    if not (
        grid.get("format") == "uint16-le"
        and grid.get("scaleMeters") == 0.5
        and grid.get("offsetMeters") == -10000
        and math.isfinite(radius)
        and radius > 0
        and grid.get("body")
        and width > 1
        and height > 1
    ):
        raise RuntimeError("Expected prepared numeric elevation grid")
    if not (16 <= size <= 256 and 2 <= gutter <= 8 and 0 <= level <= 5):
        raise RuntimeError("Invalid height grid settings (max level 5)")
    raw = source.parent / grid["data"]
    digest = sha256(raw)
    if digest != grid.get("dataSha256"):
        raise RuntimeError("Global elevation checksum mismatch")
    values = _read_values(raw, width * height)
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
    if not (out / "build-state.json").exists() and out.exists() and any(out.iterdir()):
        raise RuntimeError("Nonempty height output without build state")
    _compatible(out / "build-state.json", identity)
    manifest_path = out / "manifest.json"
    if manifest_path.exists() and len(read_json(manifest_path).get("sources", [])) > 1:
        log("Global height base already exists; preserving regional refinements.")
        return
    manifest = _empty_manifest(size, gutter, level, grid)
    tiles = manifest["tiles"]
    tile_width = size + 2 * gutter
    prefix = "lola" if grid["body"] == "Moon" else "global"
    completed = 0
    rows = np.arange(tile_width)
    cols = np.arange(tile_width)
    column, row = np.meshgrid(cols, rows)
    for face in range(6):
        for depth in range(level + 1):
            count = 1 << depth
            for y in range(count):
                for x in range(count):
                    check()
                    address = f"{face}/{depth}/{x}/{y}"
                    path = f"{prefix}/{address}.u16"
                    if (out / path).exists():
                        tile = _read_values(out / path, tile_width * tile_width)
                    else:
                        uu = 2 * (x + (column - gutter + 0.5) / size) / count - 1
                        vv = 2 * (y + 1 - (row - gutter + 0.5) / size) / count - 1
                        vectors = direction(face, uu, vv)
                        sample_u = (np.atan2(-vectors[2], vectors[0]) + PI) / (2 * PI)
                        sample_v = np.atan2(np.hypot(vectors[0], vectors[2]), vectors[1]) / PI
                        meters = _grid(
                            values,
                            width,
                            height,
                            sample_u * width - 0.5,
                            sample_v * height - 0.5,
                            wrap=True,
                        )
                        tile = _encode(meters)
                    tiles[address] = _write_height(out, path, tile)
                    completed += 1
                    if completed % 200 == 0:
                        log(f"{completed} height tiles ready")
    manifest["sources"].append(
        {
            "id": prefix,
            "credit": grid.get("credit"),
            "sourcePage": grid.get("sourcePage"),
        }
    )
    _propagate(manifest)
    _publish(out, manifest, level)
    log(f"Published {out / 'manifest.json'} ({completed} numeric height tiles)")


def height_region(args: Args):
    args.validate(
        {
            "--source",
            "--output",
            "--id",
            "--latitude",
            "--longitude",
            "--radius-meters",
            "--max-level",
            "--input-scale",
            "--input-offset",
            "--credit",
            "--source-page",
            "--coverage-config",
        }
    )
    root = Path(args.get("--output", "public/data/moon/elevation-tiles"))
    source = args.get("--source")
    region_id = args.get("--id")
    if (
        not region_id
        or region_id in ("lola", "global")
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in region_id)
    ):
        raise RuntimeError("Invalid height source ID")
    manifest = read_json(root / "manifest.json")
    body_radius = float(manifest["radiusMeters"])
    if not math.isfinite(body_radius) or body_radius <= 0:
        raise RuntimeError("Invalid body radius")
    size = int(manifest["tileSize"])
    gutter = int(manifest["gutter"])
    base_max = int(manifest["maxLevel"])
    max_level = args.integer("--max-level", 14)
    latitude = float(args.get("--latitude")) * PI / 180
    longitude = float(args.get("--longitude")) * PI / 180
    extent = float(args.get("--radius-meters", "2000"))
    scale = float(args.get("--input-scale", "1"))
    offset = float(args.get("--input-offset", "0"))
    if not (
        math.isfinite(latitude)
        and abs(latitude) <= PI / 2
        and math.isfinite(longitude)
        and abs(longitude) <= PI
        and math.isfinite(extent)
        and 100 <= extent <= 20000
        and math.isfinite(scale)
        and scale > 0
        and math.isfinite(offset)
        and 0 < max_level <= 16
    ):
        raise RuntimeError("Invalid height region settings")
    with rasterio.open(source) as src:
        if src.count != 1:
            raise RuntimeError("Height source must have one numeric band")
        require_sphere(
            src.crs,
            body_radius,
            "Height source must be georeferenced to the configured body sphere",
        )
        coverage_settings = (
            read_json(args.get("--coverage-config")) if args.has("--coverage-config") else {}
        )
        identity = {"pipelineVersion": 1}
        if args.has("--coverage-config"):
            identity["coverage"] = coverage_settings
        identity.update(
            {
                "sourceSha256": sha256(source),
                "latitude": latitude,
                "longitude": longitude,
                "radius": extent,
                "maxLevel": max_level,
                "inputScale": scale,
                "inputOffset": offset,
            }
        )
        if manifest.get("body") != "Moon":
            identity["body"] = manifest.get("body")
            identity["bodyRadius"] = body_radius
        work = root / "sources" / region_id
        _compatible(work / "state.json", identity)
        if not (work / "base.json").exists():
            atomic_json(work / "base.json", manifest)
        base = read_json(work / "base.json")
        base_tiles = base["tiles"]
        for face in range(6):
            if f"{face}/0/0/0" not in base_tiles:
                raise RuntimeError("Height base requires six roots")
        base_level = 0
        for depth in range(1, 6):
            complete = True
            for face in range(6):
                for y in range(1 << depth):
                    for x in range(1 << depth):
                        if f"{face}/{depth}/{x}/{y}" not in base_tiles:
                            complete = False
                            break
                    if not complete:
                        break
                if not complete:
                    break
            if not complete:
                break
            base_level = depth
        if max_level <= base_level:
            raise RuntimeError("Height region must refine beyond the complete global pyramid")
        loaded_pages = set()

        def load_base_page(face, u, v):
            pages = base.get("pages") or {}
            if not pages:
                return
            level = int(base["pageLevel"])
            count = 1 << level
            address = (
                f"{face}/{level}/{min(count - 1, max(0, int(u * count)))}/"
                f"{min(count - 1, max(0, int(v * count)))}"
            )
            page = pages.get(address)
            if page is None or address in loaded_pages:
                return
            frozen = work / "base-pages" / f"{address}.json"
            if not frozen.exists():
                atomic_json(frozen, read_json(root / page["path"]))
            base_tiles.update(read_json(frozen)["tiles"])
            loaded_pages.add(address)

        cache = {}
        tile_width = size + 2 * gutter

        def coarse_heights(vectors):
            # One array sample per coarse tile. The old per-pixel loop made the
            # south-pole pack call into Python about 200 million times.
            if len(cache) > 64:
                cache.clear()
            face_id, u, v = _face_uv_grid(vectors)
            if base.get("pages"):
                level = int(base["pageLevel"])
                count = 1 << level
                tx = np.clip(np.trunc(u * count).astype(np.int64), 0, count - 1)
                ty = np.clip(np.trunc(v * count).astype(np.int64), 0, count - 1)
                codes = face_id * count * count + tx * count + ty
                flat_codes = codes.ravel()
                order = np.argsort(flat_codes, kind="stable")
                sorted_codes = flat_codes[order]
                changes = np.flatnonzero(np.diff(sorted_codes, prepend=sorted_codes[0] - 1))
                flat_face = face_id.ravel()
                flat_u = u.ravel()
                flat_v = v.ravel()
                for index in order[changes]:
                    load_base_page(
                        int(flat_face[index]), float(flat_u[index]), float(flat_v[index])
                    )
            result = np.empty(u.shape, np.float64)
            pending = np.ones(u.shape, dtype=bool)
            for level in range(int(base["maxLevel"]), -1, -1):
                if not pending.any():
                    break
                count = 1 << level
                span = count * count
                tx = np.clip(np.trunc(u * count).astype(np.int64), 0, count - 1)
                ty = np.clip(np.trunc(v * count).astype(np.int64), 0, count - 1)
                codes = np.where(pending, face_id * span + tx * count + ty, -1)
                for code in np.unique(codes):
                    if code < 0:
                        continue
                    code = int(code)
                    face_value, rem = divmod(code, span)
                    x, y = divmod(rem, count)
                    key = f"{face_value}/{level}/{x}/{y}"
                    if key not in base_tiles:
                        continue
                    mask = codes == code
                    info = base_tiles[key]
                    if key not in cache:
                        cache[key] = _read_values(root / info["path"], tile_width * tile_width)
                    xs = gutter + (u[mask] * count - x) * size - 0.5
                    ys = gutter + (1 - (v[mask] * count - y)) * size - 0.5
                    result[mask] = _grid(cache[key], tile_width, tile_width, xs, ys)
                    pending[mask] = False
            if pending.any():
                raise RuntimeError("Height base is missing a tile")
            return result

        center = np.array(
            [
                math.cos(latitude) * math.cos(longitude),
                math.sin(latitude),
                -math.cos(latitude) * math.sin(longitude),
            ]
        )
        face, center_u, center_v = _face_uv(center)
        dominant = max(abs(float(part)) for part in center)
        delta = 2 * math.sin(extent / body_radius / 2)

        def interval(q):
            points = (
                (q - delta) / (dominant - delta),
                (q + delta) / (dominant - delta),
                (q - delta) / (dominant + delta),
                (q + delta) / (dominant + delta),
            )
            return (min(points) + 1) / 2, (max(points) + 1) / 2

        us = interval((center_u * 2 - 1) * dominant)
        vs = interval((center_v * 2 - 1) * dominant)
        if not (us[0] > 0 and us[1] < 1 and vs[0] > 0 and vs[1] < 1):
            raise RuntimeError("Height region crosses cube-face edge")
        addresses = []
        for level in range(base_level + 1, max_level + 1):
            count = 1 << level
            for y in range(int(vs[0] * count), int(vs[1] * count) + 1):
                for x in range(int(us[0] * count), int(us[1] * count) + 1):
                    addresses.append(Address(face, level, x, y))
        coverage = Coverage(coverage_settings, center, extent, base_level, max_level, body_radius)
        coverage.append(addresses, base_level, face)
        if len(addresses) > 20000:
            raise RuntimeError("Height region exceeds 20,000 tiles; reduce radius/depth")
        merged = set()
        pages = manifest.get("pages") or {}
        page_level = int(manifest.get("pageLevel") or 0)
        for address in addresses:
            if address.level <= page_level or not pages:
                continue
            shift = address.level - page_level
            parent = f"{address.face}/{page_level}/{address.x >> shift}/{address.y >> shift}"
            page = pages.get(parent)
            if page is None or parent in merged:
                continue
            manifest["tiles"].update(read_json(root / page["path"])["tiles"])
            merged.add(parent)
        tiles = manifest["tiles"]
        completed = 0
        rows = np.arange(tile_width)
        cols = np.arange(tile_width)
        column, row = np.meshgrid(cols, rows)
        for address in addresses:
            check()
            path = f"{region_id}/{address.key}.u16"
            if (root / path).exists():
                values = _read_values(root / path, tile_width * tile_width)
            else:
                sampled = np.zeros((2, tile_width, tile_width), np.float32)
                reproject(
                    source=rasterio.band(src, [1]),
                    destination=sampled,
                    dst_transform=tile_affine(
                        face, address.level, address.x, address.y, size, gutter, body_radius
                    ),
                    dst_crs=face_crs(face, body_radius),
                    resampling=Resampling.bilinear,
                    dst_alpha=2,
                    num_threads=1,
                    warp_mem_limit=128,
                )
                count = 2**address.level
                uu = 2 * (address.x + (column - gutter + 0.5) / size) / count - 1
                vv = 2 * (address.y + 1 - (row - gutter + 0.5) / size) / count - 1
                vectors = direction(face, uu, vv)
                coarse = coarse_heights(vectors)
                distance = body_radius * np.linalg.norm(
                    vectors - coverage.center.reshape(3, 1, 1), axis=0
                )
                fine = sampled[0] * scale + offset
                usable = (sampled[1] > 0) & np.isfinite(sampled[0])
                mix = np.where(usable, coverage.weight(distance), 0)
                values = _encode(np.where(mix > 0, coarse * (1 - mix) + fine * mix, coarse))
            tiles[address.key] = _write_height(root, path, values)
            completed += 1
            if completed % 200 == 0:
                log(f"{completed}/{len(addresses)} regional height tiles ready")
    manifest["maxLevel"] = max(base_max, max_level)
    if not any(item.get("id") == region_id for item in manifest["sources"]):
        entry = {
            "id": region_id,
            "credit": args.get("--credit", "User-supplied lunar DEM"),
            "sourcePage": args.get("--source-page"),
            "sha256": identity["sourceSha256"],
            "featherMeters": coverage.feather,
            "inputScale": scale,
            "inputOffset": offset,
        }
        if coverage.description is not None:
            entry["overview"] = coverage.description
        manifest["sources"].append(entry)
    _propagate(manifest)
    _publish(root, manifest, base_level)
    log(f"Published regional height source {region_id}")
