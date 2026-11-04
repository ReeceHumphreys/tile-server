"""Source acquisition: resumable, verified downloads and virtual mosaics.

Every downloaded source sits beside a `<file>.receipt.json` recording what was
fetched and its SHA-256. A source without a matching receipt is never reused.
"""

import ctypes
import os
import shutil
from pathlib import Path

import rasterio

from tile_service.runtime import (
    atomic_json,
    gdal_library,
    log,
    read_json,
    run,
    sha256,
    validate_imagery,
)

TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")


def _tiff(path):
    with open(path, "rb") as handle:
        return handle.read(4) in TIFF_MAGIC


def download(url, partial, etag="", last_modified=""):
    """Resume `url` into `partial` with curl, refusing a changed remote file."""
    Path(partial).parent.mkdir(parents=True, exist_ok=True)
    args = [
        "curl",
        "--fail",
        "--location",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--continue-at",
        "-",
        "--output",
        str(partial),
    ]
    if etag.startswith("W/"):
        if not last_modified:
            raise RuntimeError("Weak ETag requires sourceLastModified for safe resumption")
        args += ["--header", "If-Unmodified-Since: " + last_modified]
    elif etag:
        args += ["--header", "If-Match: " + etag]
    args.append(url)
    run(args)


def describe(entry, path=None):
    out = Path(path or entry.get("sourceFile", ""))
    size = entry.get("expectedBytes")
    lines = [entry.get("title") or entry.get("name") or entry.get("id", "")]
    if entry.get("sourceSizeNote"):
        lines.append(entry["sourceSizeNote"])
    elif size:
        lines.append(f"{int(size):,} bytes")
    lines += [entry.get("sourceUrl", ""), f"Destination: {out}"]
    return "\n".join(line for line in lines if line)


def ensure(entry, path=None):
    """Return a verified local copy of a catalog source, downloading if needed.

    `entry` provides `sourceUrl`, `expectedBytes`, and optionally `sourceETag`,
    `sourceLastModified`, a pinned `sha256`, and the default `sourceFile`.
    An existing file must match its receipt (and the pin); it is never replaced.
    """
    out = Path(path or entry["sourceFile"])
    url = entry["sourceUrl"]
    etag = entry.get("sourceETag")
    pinned = entry.get("sha256")
    expected = int(entry["expectedBytes"])
    receipt_path = Path(str(out) + ".receipt.json")
    if out.exists():
        if receipt_path.exists():
            old = read_json(receipt_path)
            digest = sha256(out)
            if (
                old.get("url") != url
                or old.get("etag") != etag
                or old.get("sha256") != digest
                or (pinned and digest != pinned)
                or ("bytes" in old and int(old["bytes"]) != expected)
            ):
                raise RuntimeError(
                    f"Existing source does not match its download receipt. No file overwritten: {out}"
                )
        elif not pinned or sha256(out) != pinned:
            raise RuntimeError(
                f"Existing source has no receipt and does not match a pinned checksum: {out}. "
                "Remove it to download again."
            )
        else:
            atomic_json(receipt_path, _receipt(entry, expected, pinned))
        log(f"Verified {out}")
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    part = Path(str(out) + ".part")
    have = part.stat().st_size if part.exists() else 0
    if shutil.disk_usage(out.parent).free < max(0, expected - have) + 64 * 1024**2:
        raise RuntimeError(f"Insufficient disk space to download {out.name}.")
    log(describe(entry, out))
    if have != expected:
        download(url, part, etag or "", entry.get("sourceLastModified") or "")
    if part.stat().st_size != expected:
        raise RuntimeError("Download size differs from the catalog; rerun to resume or review.")
    if not _tiff(part):
        raise RuntimeError("Download is not TIFF; partial file retained for inspection.")
    digest = sha256(part)
    if pinned and digest != pinned:
        raise RuntimeError("Source checksum changed; review before updating the pinned checksum.")
    # Receipt first: a crash before the final rename stays resumable.
    atomic_json(receipt_path, _receipt(entry, expected, digest))
    os.replace(part, out)
    log(f"Complete: {out}")
    return out


def _receipt(entry, expected, digest):
    return {
        "url": entry["sourceUrl"],
        "etag": entry.get("sourceETag"),
        "sourcePage": entry.get("sourcePage"),
        "bytes": expected,
        "sha256": digest,
        "checksumNote": "Locally computed integrity receipt, not a publisher-provided checksum.",
    }


def missing_bytes(entries, folder):
    """Bytes still to download for `entries` stored as `<folder>/<id>.tif`."""
    total = 0
    for entry in entries:
        have = 0
        for candidate in (folder / f"{entry['id']}.tif", folder / f"{entry['id']}.tif.part"):
            if candidate.exists():
                have = candidate.stat().st_size
                break
        total += max(0, int(entry["expectedBytes"]) - have)
    return total


def _build_vrt(paths, dest):
    library = ctypes.CDLL(str(gdal_library()))
    library.GDALAllRegister.restype = None
    library.GDALAllRegister()
    library.GDALBuildVRTOptionsNew.restype = ctypes.c_void_p
    library.GDALBuildVRTOptionsNew.argtypes = [ctypes.POINTER(ctypes.c_char_p), ctypes.c_void_p]
    library.GDALBuildVRTOptionsFree.argtypes = [ctypes.c_void_p]
    library.GDALBuildVRT.restype = ctypes.c_void_p
    library.GDALBuildVRT.argtypes = [
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    library.GDALClose.argtypes = [ctypes.c_void_p]
    library.GDALClose.restype = ctypes.c_int
    argv = (ctypes.c_char_p * 2)(b"-strict", None)
    options = library.GDALBuildVRTOptionsNew(argv, None)
    if not options:
        raise RuntimeError("Cannot configure mosaic")
    encoded = [str(path).encode() for path in paths]
    names = (ctypes.c_char_p * len(encoded))(*encoded)
    error = ctypes.c_int(0)
    result = library.GDALBuildVRT(
        str(dest).encode(),
        len(encoded),
        None,
        names,
        options,
        ctypes.byref(error),
    )
    library.GDALBuildVRTOptionsFree(options)
    if not result or error.value:
        raise RuntimeError("Cannot build mosaic")
    if library.GDALClose(result) != 0:
        raise RuntimeError("GDAL raster close failed")


def mosaic(paths, out, radius):
    """Write a virtual mosaic of same-sphere imagery; unchanged output is left alone."""
    if not 1 <= len(paths) <= 40:
        raise RuntimeError("Mosaic requires 1..40 sources")
    resolved = []
    for file in paths:
        path = Path(file).resolve()
        with rasterio.open(path) as source:
            validate_imagery(source, radius)
        resolved.append(path)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(out) + ".tmp")
    _build_vrt(resolved, temp)
    if out.exists() and out.read_bytes() == temp.read_bytes():
        temp.unlink()
    else:
        os.replace(temp, out)
    log(f"Virtual mosaic ready: {out}")
    return out


# Source adapters ---------------------------------------------------------------
#
# A Source turns a catalog description into a verified local raster. Every
# adapter offers the same interface: `entry` (the descriptive dataset record),
# `describe()` for dry runs, `fetch()` returning a local path, and
# `provenance()` for published manifests.

PROVENANCE = (
    "product",
    "sourceUrl",
    "sourcePage",
    "credit",
    "nativeResolutionMeters",
    "expectedBytes",
    "sourceETag",
)


class Download:
    """One pinned HTTP raster, resumable, verified against its receipt."""

    def __init__(self, entry, path):
        self.entry = entry
        self.path = Path(path)

    def describe(self):
        return describe(self.entry, self.path)

    def fetch(self):
        return ensure(self.entry, self.path)

    def provenance(self):
        return {key: self.entry.get(key) for key in PROVENANCE}


class Mosaic:
    """Several downloads joined by a GDAL VRT, without a second giant raster."""

    def __init__(self, parts, path, radius, raw):
        self.parts = [Download(part, Path(raw) / f"{part['id']}.tif") for part in parts]
        self.path = Path(path)
        self.radius = radius
        self.entry = parts[0]

    def describe(self):
        total = sum(int(part.entry["expectedBytes"]) for part in self.parts)
        lines = [f"Mosaic of {len(self.parts)} sources: {total / 1e9:.2f} GB"]
        lines += [f"  {part.entry['id']}: {part.entry['sourceUrl']}" for part in self.parts]
        return "\n".join(lines + [f"Destination: {self.path}"])

    def fetch(self):
        folder = self.path.parent
        folder.mkdir(parents=True, exist_ok=True)
        need = missing_bytes([part.entry for part in self.parts], folder)
        if shutil.disk_usage(folder).free < need + 512 * 1024**2:
            raise RuntimeError("Insufficient disk space for sources plus a 512 MiB reserve")
        return mosaic([part.fetch() for part in self.parts], self.path, self.radius)

    def provenance(self):
        first = self.parts[0].entry
        return {
            "sourceUrl": first.get("sourceUrl"),
            "credit": first.get("credit"),
            "components": [
                {**part.entry, "sha256": read_json(f"{part.path}.receipt.json")["sha256"]}
                for part in self.parts
            ],
        }


class Wmts:
    """One zoom level of the EOxCloudless WMTS, stitched into a GeoTIFF."""

    def __init__(self, entry, zoom, path):
        self.entry = entry
        self.zoom = zoom
        self.path = Path(path)

    def describe(self):
        from tile_service import cloudless

        return cloudless.describe(self.zoom) + f"\nDestination: {self.path}"

    def fetch(self):
        from tile_service import cloudless

        return cloudless.mosaic(self.path, zoom=self.zoom)

    def provenance(self):
        return {key: self.entry.get(key) for key in PROVENANCE}


class NaipCrop:
    """A site disk cut from pinned NAIP cloud-optimized quads."""

    def __init__(self, site, extent, max_level, folder):
        self.entry = site
        self.extent = extent
        self.max_level = max_level
        self.folder = Path(folder)

    def describe(self):
        quads = len(self.entry["sources"])
        return f"{self.entry['product']}; {quads} NAIP quad{'s' if quads != 1 else ''}"

    def fetch(self):
        from tile_service import naip

        if not naip.covers(self.entry, self.extent):
            raise RuntimeError("Pinned NAIP quads do not cover this disk; reduce the radius")
        return naip.crop(self.entry, self.extent, self.max_level, self.folder)

    def provenance(self):
        site = self.entry
        return {
            "product": site["product"],
            "sourceUrl": site["sources"][0]["href"],
            "sourcePage": site["sourcePage"],
            "credit": site["credit"],
            "nativeResolutionMeters": float(site["nativeResolutionMeters"]),
            "components": [{"product": quad["id"]} for quad in site["sources"]],
        }


class Local:
    """A raster already on disk, e.g. `build --source`."""

    def __init__(self, path, entry):
        self.path = Path(path)
        self.entry = entry

    def describe(self):
        return f"Local source: {self.path}"

    def fetch(self):
        if not self.path.is_file():
            raise RuntimeError(f"Source not found: {self.path}")
        return self.path

    def provenance(self):
        return {key: self.entry.get(key) for key in PROVENANCE}


def open_source(ws, spec, *, site=None, radius=None, extent=None, max_level=None):
    """The Source adapter a catalog `source` spec describes, placed in workspace `ws`.

    `spec["type"]` is download, mosaic, wmts or naip. A download takes its
    entry from `spec["dataset"]`, inline fields, or the site itself, and lives
    at the entry's `sourceFile` or `<raw>/<id>.tif` under `ws.sources`.
    """
    kind = spec.get("type", "download")
    if kind == "download":
        if "dataset" in spec:
            entry = ws.dataset(spec["dataset"])
        elif "sourceUrl" in spec:
            entry = {key: value for key, value in spec.items() if key not in ("type", "raw")}
        else:
            entry = site
        relative = entry.get("sourceFile") or Path(spec.get("raw", ".")) / f"{entry['id']}.tif"
        return Download(entry, ws.source_path(relative))
    if kind == "mosaic":
        raw = ws.source_path(spec["raw"])
        return Mosaic(spec["parts"], raw / spec["file"], radius, raw)
    if kind == "wmts":
        entry = ws.dataset(spec["dataset"])
        return Wmts(entry, spec["zoom"], ws.source_path(entry["sourceFile"]))
    if kind == "naip":
        return NaipCrop(site, extent, max_level, ws.source_path(spec.get("raw", "earth/sites")))
    raise RuntimeError(f"Unknown source type {kind!r}")
