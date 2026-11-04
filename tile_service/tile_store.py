"""Read cube tiles from a dataset, wherever they live.

Builds write loose files (`{prefix}{face}/{level}/{x}/{y}.{png,u16}`);
packing adds PMTiles archives and a `storage` descriptor. A TileStore reads
the loose file when it exists and otherwise the archive, so loose tiles can
be deleted once a dataset is packed and later builds still find their base.
"""

import gzip
import re
import threading
from pathlib import Path

from pmtiles.reader import Reader
from pmtiles.tile import Compression

from tile_service.runtime import atomic_write, read_json

ADDRESS = re.compile(r"^((?:[a-zA-Z0-9_-]+/)*)([0-5])/(\d+)/(\d+)/(\d+)\.(png|u16)$")


class TileStore:
    """Tiles of one dataset directory: loose files first, then PMTiles archives."""

    def __init__(self, directory, storage=None):
        self.directory = Path(directory)
        self.archives = (storage or {}).get("archives", {})
        self._readers = {}
        self._lock = threading.Lock()

    @classmethod
    def for_document(cls, path):
        """The store for a manifest, using its `storage` if it has been packed."""
        path = Path(path)
        return cls(path.parent, read_json(path).get("storage"))

    def _archive(self, key):
        if key not in self._readers:
            name = self.archives.get(key)
            if name is None:
                self._readers[key] = None
            else:
                handle = (self.directory / name).open("rb")

                def get(offset, length, handle=handle):
                    handle.seek(offset)
                    return handle.read(length)

                self._readers[key] = (Reader(get), handle)
        return self._readers[key]

    def read(self, relative):
        """Tile bytes for a relative tile path, or None when the dataset lacks it."""
        loose = self.directory / relative
        if loose.is_file():
            return loose.read_bytes()
        match = ADDRESS.fullmatch(relative)
        if not match or not self.archives:
            return None
        prefix, face, level, x, y, _ = match.groups()
        with self._lock:
            opened = self._archive(prefix + face)
            if opened is None:
                return None
            reader, _ = opened
            data = reader.get(int(level), int(x), int(y))
            if data is not None and reader.header()["tile_compression"] == Compression.GZIP:
                data = gzip.decompress(data)
        return data

    def has(self, relative):
        if (self.directory / relative).is_file():
            return True
        match = ADDRESS.fullmatch(relative)
        if not match or not self.archives:
            return False
        prefix, face, level, x, y, _ = match.groups()
        with self._lock:
            opened = self._archive(prefix + face)
            if opened is None:
                return False
            reader, _ = opened
            # A directory lookup without fetching the tile body would be cheaper;
            # pmtiles' reader exposes only get(), which is fine at this scale.
            return reader.get(int(level), int(x), int(y)) is not None

    def restore(self, paths):
        """Write deleted loose tiles back; call before a document drops its archives."""
        try:
            for relative in paths:
                if not (self.directory / relative).is_file():
                    data = self.read(relative)
                    if data is None:
                        raise RuntimeError(f"Tile missing: {relative} in {self.directory}")
                    atomic_write(self.directory / relative, data)
        finally:
            self.close()

    def close(self):
        for opened in self._readers.values():
            if opened:
                opened[1].close()
        self._readers.clear()
