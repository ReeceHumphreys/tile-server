import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.warp import reproject

from pipeline.runtime import (
    PI,
    Args,
    atomic_json,
    check,
    config,
    direction,
    face_crs,
    gdal_version,
    log,
    read_json,
    sha256,
    tile_affine,
    validate_source,
    write_png,
)


class Address:
    def __init__(self, face, level, x, y):
        self.face = face
        self.level = level
        self.x = x
        self.y = y

    @property
    def key(self):
        return f"{self.face}/{self.level}/{self.x}/{self.y}"


def _face_of(center):
    axis = int(np.argmax(np.abs(center)))
    positive = center[axis] > 0
    if axis == 0:
        return 0 if positive else 1
    if axis == 1:
        return 2 if positive else 3
    return 4 if positive else 5


def _local(face, center):
    if face == 0:
        return -center[2], center[1], center[0]
    if face == 1:
        return center[2], center[1], -center[0]
    if face == 2:
        return center[0], -center[2], center[1]
    if face == 3:
        return center[0], center[2], -center[1]
    if face == 4:
        return center[0], center[1], center[2]
    return -center[0], center[1], -center[2]


def _interval(q, w, delta):
    points = (
        (q - delta) / (w - delta),
        (q + delta) / (w - delta),
        (q - delta) / (w + delta),
        (q + delta) / (w + delta),
    )
    return (min(points) + 1) / 2, (max(points) + 1) / 2


class Coverage:
    def __init__(self, settings, inner, inner_radius, base_level, max_level, body_radius):
        self.center = np.array(inner, dtype=float)
        self.radius = inner_radius
        self.feather = min(100.0, inner_radius * 0.1)
        self.level = 0
        self.face = -1
        self.u = (0.0, 0.0)
        self.v = (0.0, 0.0)
        self.description = None
        overview = settings.get("overview")
        if overview:
            latitude = float(overview["latitudeDeg"])
            longitude = float(overview["longitudeDeg"])
            self.radius = float(overview["radiusMeters"])
            self.level = int(overview["maxLevel"])
            if not (
                math.isfinite(latitude)
                and abs(latitude) <= 90
                and math.isfinite(longitude)
                and abs(longitude) <= 180
                and math.isfinite(self.radius)
                and self.radius >= inner_radius
                and self.radius <= 100000
                and base_level < self.level <= max_level
            ):
                raise RuntimeError("Invalid overview coverage")
            lat = latitude * PI / 180
            lon = longitude * PI / 180
            self.center = np.array(
                [math.cos(lat) * math.cos(lon), math.sin(lat), -math.cos(lat) * math.sin(lon)]
            )
            if (
                body_radius * np.linalg.norm(self.center - np.array(inner)) + inner_radius
                > self.radius
            ):
                raise RuntimeError("Overview must contain the detail region")
            self.face = _face_of(self.center)
            uu, vv, ww = _local(self.face, self.center)
            delta = 2 * math.sin(self.radius / body_radius / 2)
            self.u = _interval(uu, ww, delta)
            self.v = _interval(vv, ww, delta)
            if not (self.u[0] > 0 and self.u[1] < 1 and self.v[0] > 0 and self.v[1] < 1):
                raise RuntimeError("Overview crosses a cube-face boundary")
            self.description = overview
        if "featherMeters" in settings:
            self.feather = float(settings["featherMeters"])
        if not (
            math.isfinite(self.feather) and self.feather > 0 and self.feather <= self.radius * 0.5
        ):
            raise RuntimeError("Invalid coverage feather")

    def append(self, addresses, base_level, detail_face):
        if not self.level:
            return
        if detail_face != self.face:
            raise RuntimeError("Overview and detail must share one cube face")
        for level in range(base_level + 1, self.level + 1):
            count = 1 << level
            y0, y1 = int(self.v[0] * count), int(self.v[1] * count)
            x0, x1 = int(self.u[0] * count), int(self.u[1] * count)
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    addresses.append(Address(self.face, level, x, y))
                    if len(addresses) > 40000:
                        raise RuntimeError("Region exceeds 20,000 tiles per layer")
        addresses.sort(key=lambda item: (item.level, item.face, item.y, item.x))
        unique = []
        seen = set()
        for item in addresses:
            if item.key not in seen:
                seen.add(item.key)
                unique.append(item)
                if len(unique) > 20000:
                    raise RuntimeError("Region exceeds 20,000 tiles per layer")
        addresses[:] = unique

    def weight(self, distance):
        blend = np.clip((self.radius - np.asarray(distance, dtype=float)) / self.feather, 0, 1)
        if self.level:
            return blend * blend * (3 - 2 * blend)
        return blend


def _read_image(path, size):
    with rasterio.open(path) as dataset:
        if dataset.width != size or dataset.height != size or dataset.count != 4:
            raise RuntimeError("Base tile dimensions/channels do not match manifest")
        planes = dataset.read()
    return np.ascontiguousarray(np.moveaxis(planes, 0, -1))


def _bilinear(image, xs, ys, band):
    limit = image.shape[0] - 1
    xs = np.clip(xs, 0, limit)
    ys = np.clip(ys, 0, limit)
    x0 = np.floor(xs).astype(np.int32)
    y0 = np.floor(ys).astype(np.int32)
    x1 = np.minimum(x0 + 1, limit)
    y1 = np.minimum(y0 + 1, limit)
    fx = xs - x0
    fy = ys - y0
    plane = image[:, :, band]
    return (1 - fy) * ((1 - fx) * plane[y0, x0] + fx * plane[y0, x1]) + fy * (
        (1 - fx) * plane[y1, x0] + fx * plane[y1, x1]
    )


def _warp_tile(src, address, width, radius, size, gutter):
    frame = np.zeros((src.count + 1, width, width), np.uint8)
    reproject(
        source=rasterio.band(src, list(range(1, src.count + 1))),
        destination=frame,
        src_nodata=src.nodata,
        dst_transform=tile_affine(
            address.face, address.level, address.x, address.y, size, gutter, radius
        ),
        dst_crs=face_crs(address.face, radius),
        resampling=Resampling.average,
        dst_alpha=src.count + 1,
        num_threads=1,
        warp_mem_limit=128,
    )
    return frame


def _pixel_grid(address, width, size, gutter):
    rows, cols = np.indices((width, width))
    scale = 2**address.level
    uu = 2 * (address.x + (cols - gutter + 0.5) / size) / scale - 1
    vv = 2 * (address.y + 1 - (rows - gutter + 0.5) / size) / scale - 1
    return rows, cols, direction(address.face, uu, vv)


# shortcut: a single gain, not a spatially varying photometric fit. Upgrade when
# a site still shows a brightness ramp the mean cannot remove.
def _match_gain(
    src, bands, addresses, base_path, stats_level, base_level, size, gutter, radius, coverage
):
    width = size + 2 * gutter
    total_over = 0.0
    total_under = 0.0
    count = 0
    backgrounds = {}
    for address in addresses:
        if address.level != stats_level:
            continue
        check()
        frame = _warp_tile(src, address, width, radius, size, gutter)
        factor = 1 << (address.level - base_level)
        local_x = address.x % factor
        local_y = address.y % factor
        ancestor = Address(
            address.face,
            base_level,
            address.x >> (address.level - base_level),
            address.y >> (address.level - base_level),
        )
        if ancestor.key not in backgrounds:
            backgrounds[ancestor.key] = _read_image(base_path.parent / f"{ancestor.key}.png", width)
        fallback = backgrounds[ancestor.key]
        rows, cols, vectors = _pixel_grid(address, width, size, gutter)
        alpha = frame[bands]
        valid = alpha >= 250
        distance = radius * np.linalg.norm(vectors - coverage.center.reshape(3, 1, 1), axis=0)
        inside = (coverage.radius - distance) / coverage.feather >= 0.98
        mask = valid & inside
        if not np.any(mask):
            continue
        px = gutter + (local_x * size + cols - gutter + 0.5) / factor - 0.5
        py = gutter + ((factor - 1 - local_y) * size + rows - gutter + 0.5) / factor - 0.5
        used = 1 if bands == 1 else 3
        over = np.zeros(mask.shape, np.float64)
        under = np.zeros(mask.shape, np.float64)
        for band in range(used):
            source_band = 0 if bands == 1 else band
            over += frame[source_band]
            under += _bilinear(fallback, px, py, band)
        total_over += float((over[mask] / used).sum())
        total_under += float((under[mask] / used).sum())
        count += int(mask.sum())
    if count < 16 or total_over <= 0:
        return 1.0
    return min(4.0, max(0.05, total_under / total_over))


def _write_region_tile(
    src, bands, address, path, fallback, base_level, size, gutter, radius, coverage, gain
):
    width = size + 2 * gutter
    frame = _warp_tile(src, address, width, radius, size, gutter)
    factor = 1 << (address.level - base_level)
    local_x = address.x % factor
    local_y = address.y % factor
    rows, cols, vectors = _pixel_grid(address, width, size, gutter)
    px = gutter + (local_x * size + cols - gutter + 0.5) / factor - 0.5
    py = gutter + ((factor - 1 - local_y) * size + rows - gutter + 0.5) / factor - 0.5
    distance = radius * np.linalg.norm(vectors - coverage.center.reshape(3, 1, 1), axis=0)
    feather = np.clip((coverage.radius - distance) / coverage.feather, 0, 1)
    if coverage.level:
        feather = feather * feather * (3 - 2 * feather)
    alpha = frame[bands] / 255 * feather
    under_alpha = _bilinear(fallback, px, py, 3) / 255
    final_alpha = alpha + under_alpha * (1 - alpha)
    output = np.zeros((4, width, width), np.uint8)
    for band in range(3):
        under = _bilinear(fallback, px, py, band)
        over = frame[0 if bands == 1 else band] * gain
        mixed = np.where(
            final_alpha > 0,
            (over * alpha + under * under_alpha * (1 - alpha)) / np.maximum(final_alpha, 1e-12),
            0,
        )
        output[band] = np.clip(np.rint(mixed), 0, 255).astype(np.uint8)
    output[3] = np.clip(np.rint(final_alpha * 255), 0, 255).astype(np.uint8)
    write_png(path, output, 3)


def disk_addresses(latitude, longitude, extent, radius, base_level, max_level):
    if not base_level < max_level <= 16:
        raise RuntimeError("Regional max level must exceed global depth and be at most 16")
    if not (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and math.isfinite(extent)
        and abs(latitude) <= 90
        and abs(longitude) <= 180
        and 100 <= extent <= 20000
    ):
        raise RuntimeError("Invalid regional center/radius")
    lat = latitude * PI / 180
    lon = longitude * PI / 180
    center = np.array(
        [math.cos(lat) * math.cos(lon), math.sin(lat), -math.cos(lat) * math.sin(lon)]
    )
    face = _face_of(center)
    u, v, w = _local(face, center)
    delta = 2 * math.sin(extent / radius / 2)
    us = _interval(u, w, delta)
    vs = _interval(v, w, delta)
    if not (us[0] > 0 and us[1] < 1 and vs[0] > 0 and vs[1] < 1):
        raise RuntimeError("Region crosses a cube face boundary; reduce its radius")
    addresses = []
    for level in range(base_level + 1, max_level + 1):
        count = 1 << level
        pending = (math.floor(us[1] * count) - math.floor(us[0] * count) + 1) * (
            math.floor(vs[1] * count) - math.floor(vs[0] * count) + 1
        )
        if len(addresses) + pending > 20000:
            raise RuntimeError("Region exceeds 20,000 tiles per layer; lower level or radius")
        for y in range(math.floor(vs[0] * count), math.floor(vs[1] * count) + 1):
            for x in range(math.floor(us[0] * count), math.floor(us[1] * count) + 1):
                addresses.append(Address(face, level, x, y))
    return center, addresses


def region(args: Args):
    args.validate(
        {
            "--config",
            "--source",
            "--base",
            "--output",
            "--latitude",
            "--longitude",
            "--radius-meters",
            "--max-level",
        }
    )
    source_config = config(args)
    region_id = source_config.get("id") or ""
    if not region_id or any(
        char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in region_id
    ):
        raise RuntimeError("Invalid region ID")
    source_path = args.get("--source")
    base_path = Path(args.get("--base"))
    root = Path(args.get("--output", "public/data/moon/apollo"))
    out = root / region_id
    base = read_json(base_path)
    radius = float(base["radiusMeters"])
    size = int(base["tileSize"])
    gutter = int(base["gutter"])
    base_level = int(base["maxLevel"])
    if not (
        base.get("projection") == "normalized-cube"
        and math.isfinite(radius)
        and radius > 0
        and 16 <= size <= 2048
        and 1 <= gutter <= 16
        and 0 <= base_level <= 15
    ):
        raise RuntimeError("Invalid base imagery manifest")
    native = float(source_config.get("nativeResolutionMeters") or 1)
    default_level = int(math.ceil(math.log2(2 * radius / (size * native))))
    max_level = args.integer("--max-level", default_level)
    latitude = float(args.get("--latitude"))
    longitude = float(args.get("--longitude"))
    extent = float(args.get("--radius-meters", "2000"))
    center, addresses = disk_addresses(latitude, longitude, extent, radius, base_level, max_level)
    face = addresses[0].face
    coverage = Coverage(source_config, center, extent, base_level, max_level, radius)
    coverage.append(addresses, base_level, face)
    if len(addresses) > 20000:
        raise RuntimeError("Region exceeds 20,000 tile safety limit; reduce radius or depth")
    with rasterio.open(source_path) as src:
        validate_source(src, radius)
        bands = src.count
        identity = {
            "pipelineVersion": 1,
            "sourceSha256": sha256(source_path),
            "baseManifestSha256": sha256(base_path),
            "latitudeDeg": latitude,
            "longitudeDeg": longitude,
            "radiusMeters": extent,
            "maxLevel": max_level,
            "sourceConfig": source_config,
            "gdal": gdal_version(),
        }
        out.mkdir(parents=True, exist_ok=True)
        state = out / "build-state.json"
        if state.exists():
            if read_json(state) != identity:
                raise RuntimeError(
                    "Region output belongs to another source/settings; choose another output directory"
                )
        elif any(out.iterdir()):
            raise RuntimeError("Nonempty region without build-state.json")
        else:
            atomic_json(state, identity)
        backgrounds = {}
        keys = []
        stats_level = base_level + 1
        while stats_level < max_level and 2 * radius / ((1 << stats_level) * size) > 80:
            stats_level += 1
        gain = _match_gain(
            src,
            bands,
            addresses,
            base_path,
            stats_level,
            base_level,
            size,
            gutter,
            radius,
            coverage,
        )
        log(f"{region_id} brightness scale {gain}")
        for index, address in enumerate(addresses, start=1):
            check()
            path = out / f"{address.key}.png"
            if not path.exists():
                shift = address.level - base_level
                ancestor = Address(face, base_level, address.x >> shift, address.y >> shift)
                if ancestor.key not in backgrounds:
                    backgrounds[ancestor.key] = _read_image(
                        base_path.parent / f"{ancestor.key}.png", size + 2 * gutter
                    )
                _write_region_tile(
                    src,
                    bands,
                    address,
                    path,
                    backgrounds[ancestor.key],
                    base_level,
                    size,
                    gutter,
                    radius,
                    coverage,
                    gain,
                )
            keys.append(address.key)
            if index % 100 == 0:
                log(f"{index}/{len(addresses)} regional tiles ready")
    published = {
        "id": region_id,
        "name": source_config.get("name"),
        "latitudeDeg": latitude,
        "longitudeDeg": longitude,
        "radiusMeters": extent,
        "maxLevel": max_level,
    }
    if coverage.description is not None:
        published["overview"] = coverage.description
    published["featherMeters"] = coverage.feather
    provenance = {
        key: source_config.get(key)
        for key in (
            "product",
            "sourceUrl",
            "sourcePage",
            "credit",
            "nativeResolutionMeters",
            "expectedBytes",
            "sourceETag",
        )
    }
    if "components" in source_config:
        provenance["components"] = source_config["components"]
    provenance["colorMatchGain"] = gain
    provenance["sha256"] = identity["sourceSha256"]
    published["source"] = provenance
    published["tiles"] = keys
    atomic_json(out / "manifest.json", published)
    catalog = {
        "version": 1,
        "body": base.get("body"),
        "radiusMeters": radius,
        "baseId": base.get("id"),
        "baseMaxLevel": base_level,
        "tileSize": size,
        "gutter": gutter,
        "sites": [],
    }
    catalog_path = root / "catalog.json"
    if catalog_path.exists():
        old = read_json(catalog_path)
        if (
            old.get("baseId") != base.get("id")
            or old.get("baseMaxLevel") != base_level
            or old.get("tileSize") != size
            or old.get("gutter") != gutter
        ):
            raise RuntimeError("Existing regional catalog uses a different base dataset")
        replaced = source_config.get("replacesRegion")
        catalog["sites"] = [
            site for site in old.get("sites", []) if site.get("id") not in (region_id, replaced)
        ]
    catalog["sites"].append(published)
    atomic_json(catalog_path, catalog)
    log(f"Published {catalog_path} ({len(addresses)} tiles for {region_id})")
