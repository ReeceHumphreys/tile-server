"""Build targets: a Layer built from a Source, named after its output.

`datasets/catalog.json` maps each target name (its output directory under
the build root) to a `layer` and its settings:

  imagery          a global PNG cube pyramid          source
  grid             a global equirectangular DEM grid  source, width, height
  heights          a global uint16 height pyramid     grid (a grid target)
  imagery-regions  detail disks over imagery          base, sites
  height-regions   detail disks into heights          base, sites

A `source` names a Source adapter (download, mosaic, wmts, naip); regional
sites may carry their own. Adding a body, site or pack is a JSON change.
Every layer resumes, and rebuilding a finished target only verifies it.
Paths come from a `Workspace`; call these through it.
"""

from dataclasses import dataclass
from pathlib import Path

from tile_service import grids, heights, imagery, regions
from tile_service.cube import Coverage
from tile_service.runtime import check, log, read_json
from tile_service.sources import Local, open_source


@dataclass
class Options:
    output: Path | None = None
    source: Path | None = None
    max_level: int | None = None
    radius_meters: float | None = None
    sites: tuple = ()
    jobs: int = 2
    dry_run: bool = False


def _output(ws, name, spec):
    """Regional heights write into their base; everything else into its own name."""
    default = spec["base"] if spec["layer"] == "height-regions" else name
    return ws.builds / spec.get("output", default)


def _sites(ws, spec, chosen=()):
    listed = ws.dataset(spec["sites"])["sites"]
    if not chosen:
        return listed
    wanted = {site.lower().replace(" ", "") for site in chosen}
    unknown = wanted - {site["id"] for site in listed}
    if unknown:
        names = ", ".join(site["id"] for site in listed)
        raise RuntimeError(f"Unknown site {', '.join(sorted(unknown))}; choose from {names}")
    return [site for site in listed if site["id"] in wanted]


def status(ws, name):
    """Status of a target in workspace `ws`: "built", "n/m" sites, or "-"."""
    spec = ws.targets()[name]
    out = _output(ws, name, spec)
    layer = spec["layer"]
    if not layer.endswith("-regions"):
        return "built" if (out / "manifest.json").exists() else "-"
    document = out / ("catalog.json" if layer == "imagery-regions" else "manifest.json")
    if not document.exists():
        return "-"
    listed = read_json(document)
    have = {item["id"] for item in listed.get("sites", listed.get("sources", []))}
    want = {site["id"] for site in _sites(ws, spec)}
    found = len(want & have)
    return "built" if found == len(want) else f"{found}/{len(want)}" if found else "-"


def build(ws, name, **options):
    """Build one target. Options: output, source, max_level, radius_meters,
    sites, jobs, dry_run (describe sources and outputs without building)."""
    every = ws.targets()
    if name not in every:
        raise RuntimeError(f"Unknown target {name!r}; run `tile-service list`")
    spec = every[name]
    opts = Options(**options)
    out = Path(opts.output) if opts.output else _output(ws, name, spec)
    layer = spec["layer"]
    if opts.source and layer not in ("imagery", "grid"):
        raise RuntimeError(f"--source applies to imagery and grid targets, not {layer}")
    if opts.sites and not layer.endswith("-regions"):
        raise RuntimeError(f"--site applies to regional targets, not {layer}")
    log(f"== {name}: {spec.get('description', layer)}")
    LAYERS[layer](ws, spec, out, opts)


def _global_source(ws, spec, opts):
    source = open_source(ws, spec["source"])
    return Local(opts.source, source.entry) if opts.source else source


def _imagery(ws, spec, out, opts):
    source = _global_source(ws, spec, opts)
    if opts.dry_run:
        log(f"{source.describe()}\nOutput: {out}")
        return
    imagery.build(source.fetch(), out, source.entry, max_level=opts.max_level, jobs=opts.jobs)


def _grid(ws, spec, out, opts):
    source = _global_source(ws, spec, opts)
    entry = source.entry
    if opts.dry_run:
        size = "" if entry.get("encoded") else f"\nGrid: {spec['width']} x {spec['height']}"
        log(f"{source.describe()}{size}\nOutput: {out}")
        return
    path = source.fetch()
    if entry.get("encoded"):
        # Already in the shared uint16 encoding: copy, don't resample.
        grids.lola(path, out, entry)
        return
    grids.warp(
        path,
        out,
        body=entry["body"],
        radius=entry["radiusMeters"],
        width=spec["width"],
        height=spec["height"],
        input_scale=entry.get("inputScale", 1),
        input_offset=entry.get("inputOffset", 0),
        credit=entry.get("credit", ""),
        source_page=entry.get("sourcePage", ""),
        geographic=spec.get("geographic", False),
    )


def _heights(ws, spec, out, opts):
    grid = ws.builds / spec["grid"] / "manifest.json"
    level = opts.max_level if opts.max_level is not None else spec["maxLevel"]
    if opts.dry_run:
        log(f"Grid: {grid}\nTile size {spec['tileSize']}, levels 0..{level}\nOutput: {out}")
        return
    heights.build(grid, out, tile_size=spec["tileSize"], max_level=level)


def _body_radius(ws, name):
    """A target's body radius, following its base or grid back to a source dataset."""
    spec = ws.targets()[name]
    if "source" in spec:
        return float(open_source(ws, spec["source"]).entry["radiusMeters"])
    return _body_radius(ws, spec.get("base") or spec["grid"])


_PROVENANCE_KEYS = {"product", "sourcePage", "credit", "nativeResolutionMeters"}


class _Region:
    """One regional site, resolved: where, how deep, what source, what provenance."""

    def __init__(self, ws, spec, site, opts, radius):
        self.site = site
        self.latitude = float(site["latitudeDeg"])
        self.longitude = float(site["longitudeDeg"])
        default = site.get("radiusMeters", spec.get("radiusMeters", 2000))
        self.extent = float(opts.radius_meters if opts.radius_meters is not None else default)
        if not 100 <= self.extent <= 20000:
            raise RuntimeError("Radius must be 100..20000 meters")
        self.level = opts.max_level if opts.max_level is not None else site.get("maxLevel")
        if self.level is not None and not 1 <= self.level <= 16:
            raise RuntimeError("Max level must be 1..16")
        self.overview = site.get("overview")
        if self.overview and self.level is not None:
            self.overview = {
                **self.overview,
                "maxLevel": min(self.level, self.overview["maxLevel"]),
            }
        self.feather = site.get("featherMeters")
        source_spec = {**spec.get("source", {}), **site.get("source", {})}
        self.source = open_source(
            ws, source_spec, site=site, radius=radius, extent=self.extent, max_level=self.level
        )

    def plan(self, radius):
        """Upper bound on generated tiles, checked before any download."""
        if self.level is None:
            return None
        coverage = Coverage(
            self.latitude,
            self.longitude,
            self.extent,
            radius,
            0,
            self.level,
            overview=self.overview,
            feather=self.feather,
        )
        return len(coverage.tiles)

    def describe(self, radius):
        planned = self.plan(radius)
        depth = f"max level {self.level}" if self.level is not None else "native depth"
        lines = [
            f"{self.site.get('name', self.site['id'])}: {self.latitude}°, {self.longitude}°; "
            f"radius {self.extent:g} m; {depth}",
            self.source.describe(),
        ]
        if planned is not None:
            lines.append(f"Planned addresses: at most {planned:,}.")
        if self.site.get("detail"):
            lines.append(self.site["detail"])
        return "\n".join(lines)

    def config(self):
        """The region description and provenance `regions.build` publishes."""
        described = {
            key: self.site[key]
            for key in ("id", "name", "replacesRegion", "featherMeters")
            if key in self.site
        }
        if self.overview:
            described["overview"] = self.overview
        overrides = {key: self.site[key] for key in self.site if key in _PROVENANCE_KEYS}
        return {**described, **self.source.provenance(), **overrides}


def _regions(ws, spec, out, opts):
    radius = _body_radius(ws, spec["base"])
    base = ws.builds / spec["base"] / "manifest.json"
    for site in _sites(ws, spec, opts.sites):
        check()
        region = _Region(ws, spec, site, opts, radius)
        log(region.describe(radius))
        if opts.dry_run:
            continue
        regions.build(
            region.source.fetch(),
            base,
            out,
            region.config(),
            region.latitude,
            region.longitude,
            region.extent,
            region.level,
        )


def _height_regions(ws, spec, out, opts):
    radius = _body_radius(ws, spec["base"])
    for site in _sites(ws, spec, opts.sites):
        check()
        region = _Region(ws, spec, site, opts, radius)
        log(region.describe(radius))
        if opts.dry_run:
            continue
        base = read_json(out / "manifest.json")
        if base.get("projection") != "normalized-cube" or base.get("format") != "uint16-le":
            raise RuntimeError("Expected a numeric cube height base")
        level = region.level or regions.default_level(base, site["nativeResolutionMeters"])
        entry = region.source.entry
        heights.add_region(
            out,
            region.source.fetch(),
            site["id"],
            region.latitude,
            region.longitude,
            region.extent,
            max_level=level,
            input_scale=entry.get("inputScale", 1),
            input_offset=entry.get("inputOffset", 0),
            credit=site.get("credit") or entry.get("credit"),
            source_page=site.get("sourcePage") or entry.get("sourcePage"),
            overview=region.overview,
            feather=region.feather,
        )


LAYERS = {
    "imagery": _imagery,
    "grid": _grid,
    "heights": _heights,
    "imagery-regions": _regions,
    "height-regions": _height_regions,
}
