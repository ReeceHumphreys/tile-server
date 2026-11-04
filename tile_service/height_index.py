"""The on-disk height pyramid: `manifest.json`, paged tile metadata, and uint16 tiles.

A height index maps tile keys (`face/level/x/y`) to tile files with
conservative `lower`/`upper` bounds. Coarse levels sit inline in the manifest;
finer levels live in checksummed pages under `metadata/`, loaded on demand.
Everything that reads or writes that format goes through `HeightIndex`.
"""

from pathlib import Path

import numpy as np

from tile_service import documents
from tile_service.cube import (
    Tile,
    decode_heights,
    face_uv,
    height_range,
)
from tile_service.runtime import atomic_json, atomic_write, read_json, sha256
from tile_service.tile_store import TileStore


def read_values(path, count):
    data = Path(path).read_bytes()
    if len(data) != count * 2:
        raise RuntimeError("Incorrect height tile byte count")
    return np.frombuffer(data, dtype="<u2").copy()


def bilinear(values, width, height, xs, ys, wrap=False):
    """Meters at fractional pixel positions of an encoded uint16 grid."""
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

    return decode_heights(
        (1 - fy) * ((1 - fx) * at(ix, iy) + fx * at(ix + 1, iy))
        + fy * ((1 - fx) * at(ix, iy + 1) + fx * at(ix + 1, iy + 1))
    )


class HeightIndex:
    """One height pyramid rooted at a directory.

    Open an existing pyramid with `open`, start one with `create`, or take a
    read-only snapshot with `frozen`. `put` adds tiles, `publish` propagates
    bounds, pages fine levels and atomically replaces the manifest.
    """

    def __init__(self, root, document, read_page=None):
        self.root = Path(root)
        self.document = document
        self._read_page = read_page or (lambda address, page: read_json(self.root / page["path"]))
        self._merged = set()
        self._cache = {}
        self._store = None

    @classmethod
    def create(cls, root, *, body, radius, tile_size, gutter, max_level):
        return cls(
            root,
            documents.height_manifest(
                body=body, radius=radius, tile_size=tile_size, gutter=gutter, max_level=max_level
            ),
        )

    @classmethod
    def open(cls, root):
        return cls(root, read_json(Path(root) / "manifest.json"))

    @classmethod
    def frozen(cls, root, work, document):
        """The pyramid as it was when `work` first saw it.

        A resumed regional build samples the same coarse heights even after
        other regions have changed the live manifest and pages.
        """
        root, work = Path(root), Path(work)
        if not (work / "base.json").exists():
            atomic_json(work / "base.json", document)

        def read_page(address, page):
            # Freeze each page the first time it is read.
            frozen = work / "base-pages" / f"{address}.json"
            if not frozen.exists():
                atomic_json(frozen, read_json(root / page["path"]))
            return read_json(frozen)

        return cls(root, read_json(work / "base.json"), read_page)

    # Properties ------------------------------------------------------------

    @property
    def tile_size(self):
        return int(self.document["tileSize"])

    @property
    def gutter(self):
        return int(self.document["gutter"])

    @property
    def tile_width(self):
        return self.tile_size + 2 * self.gutter

    @property
    def radius(self):
        return float(self.document["radiusMeters"])

    @property
    def body(self):
        return self.document.get("body")

    @property
    def max_level(self):
        return int(self.document["maxLevel"])

    @property
    def tiles(self):
        """Inline tiles plus any pages merged so far."""
        return self.document["tiles"]

    def has_source(self, source_id):
        return any(item.get("id") == source_id for item in self.document["sources"])

    def add_source(self, entry):
        if not self.has_source(entry["id"]):
            self.document["sources"].append(entry)

    # Reading -----------------------------------------------------------------

    def _merge_page(self, address):
        pages = self.document.get("pages") or {}
        if address in self._merged or address not in pages:
            return
        self.tiles.update(self._read_page(address, pages[address])["tiles"])
        self._merged.add(address)

    def load(self, tiles):
        """Merge the pages that hold `tiles` into the inline tiles."""
        if not self.document.get("pages"):
            return
        page_level = int(self.document.get("pageLevel") or 0)
        for tile in tiles:
            if tile.level > page_level:
                self._merge_page(tile.ancestor(page_level).key)

    def all_tiles(self):
        """Every tile, verifying page checksums: {key: info}."""
        tiles = dict(self.document["tiles"])
        root = self.root.resolve()
        for page in self.document.get("pages", {}).values():
            path = (self.root / page["path"]).resolve()
            if not path.is_relative_to(root):
                raise RuntimeError(f"Path escapes dataset: {page['path']}")
            if sha256(path) != page["sha256"]:
                raise RuntimeError(f"Height metadata checksum mismatch: {path}")
            for key, info in read_json(path)["tiles"].items():
                if key in tiles:
                    raise RuntimeError(f"Duplicate height address: {key}")
                tiles[key] = info
        return tiles

    def read(self, path):
        """A tile's encoded values, from its loose file or the packed archives."""
        if self._store is None:
            self._store = TileStore(self.root, self.document.get("storage"))
        data = self._store.read(path)
        if data is None:
            raise RuntimeError(f"Height tile missing: {path} in {self.root}")
        if len(data) != self.tile_width * self.tile_width * 2:
            raise RuntimeError("Incorrect height tile byte count")
        return np.frombuffer(data, dtype="<u2").copy()

    def complete_level(self):
        """Deepest level at which every tile of all six faces is present inline."""
        for face in range(6):
            if f"{face}/0/0/0" not in self.tiles:
                raise RuntimeError("Height base requires six roots")
        complete = 0
        for depth in range(1, 6):
            count = 1 << depth
            if any(
                f"{face}/{depth}/{x}/{y}" not in self.tiles
                for face in range(6)
                for y in range(count)
                for x in range(count)
            ):
                break
            complete = depth
        return complete

    def sample(self, vectors):
        """Meters at each direction, from the finest tile that covers it."""
        # One array sample per coarse tile. The old per-pixel loop made the
        # south-pole pack call into Python about 200 million times.
        if len(self._cache) > 64:
            self._cache.clear()
        face_id, u, v = face_uv(vectors)
        if self.document.get("pages"):
            level = int(self.document["pageLevel"])
            count = 1 << level
            tx = np.clip(np.trunc(u * count).astype(np.int64), 0, count - 1)
            ty = np.clip(np.trunc(v * count).astype(np.int64), 0, count - 1)
            for code in np.unique(face_id * count * count + tx * count + ty):
                face_value, rem = divmod(int(code), count * count)
                x, y = divmod(rem, count)
                self._merge_page(f"{face_value}/{level}/{x}/{y}")
        size, gutter, width = self.tile_size, self.gutter, self.tile_width
        result = np.empty(u.shape, np.float64)
        pending = np.ones(u.shape, dtype=bool)
        for level in range(self.max_level, -1, -1):
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
                face_value, rem = divmod(int(code), span)
                x, y = divmod(rem, count)
                key = f"{face_value}/{level}/{x}/{y}"
                if key not in self.tiles:
                    continue
                mask = codes == code
                if key not in self._cache:
                    self._cache[key] = self.read(self.tiles[key]["path"])
                xs = gutter + (u[mask] * count - x) * size - 0.5
                ys = gutter + (1 - (v[mask] * count - y)) * size - 0.5
                result[mask] = bilinear(self._cache[key], width, width, xs, ys)
                pending[mask] = False
        if pending.any():
            raise RuntimeError("Height base is missing a tile")
        return result

    # Writing ---------------------------------------------------------------

    def put(self, key, path, values):
        """Record a tile, writing its file unless it already exists."""
        target = self.root / path
        if not target.exists():
            atomic_write(target, np.ascontiguousarray(values.astype("<u2")).tobytes())
        low, high = height_range(values)
        self.tiles[key] = {
            "path": path,
            "min": low,
            "max": high,
            "lower": low,
            "upper": high,
            "sha256": sha256(target),
        }

    def _propagate(self):
        tiles = self.tiles
        for level in range(self.max_level, 0, -1):
            for key, child in list(tiles.items()):
                tile = Tile.parse(key)
                if tile.level != level:
                    continue
                parent = tiles.get(tile.ancestor(level - 1).key)
                if parent is None:
                    raise RuntimeError("Height hierarchy missing parent")
                parent["lower"] = min(parent["lower"], child["lower"])
                parent["upper"] = max(parent["upper"], child["upper"])
        roots = [tiles[f"{face}/0/0/0"] for face in range(6)]
        self.document["minMeters"] = min(root["lower"] for root in roots)
        self.document["maxMeters"] = max(root["upper"] for root in roots)

    def publish(self, page_level):
        """Propagate bounds, page levels below `page_level`, and replace the manifest."""
        self._propagate()
        document = self.document
        # Generated files have changed; old archive references must not stay active.
        # `tile-service release` packs this content again, which needs every loose tile.
        storage = document.pop("storage", None)
        tiles = self.tiles
        pages = document.get("pages") or {}
        groups = {}
        for key, info in list(tiles.items()):
            tile = Tile.parse(key)
            if tile.level > page_level:
                groups.setdefault(tile.ancestor(page_level).key, {})[key] = info
                del tiles[key]
        for parent, entries in groups.items():
            path = f"metadata/{parent}.json"
            atomic_json(self.root / path, {"tiles": entries})
            pages[parent] = {
                "path": path,
                "sha256": sha256(self.root / path),
                "lower": tiles[parent]["lower"],
                "upper": tiles[parent]["upper"],
            }
        document["pages"] = pages
        document["pageLevel"] = page_level
        documents.validate(document)
        if storage:
            paths = [info["path"] for info in self.all_tiles().values()]
            TileStore(self.root, storage).restore(paths)
        atomic_json(self.root / "manifest.json", document)
        return document
