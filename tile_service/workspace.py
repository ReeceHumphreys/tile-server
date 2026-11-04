"""A workspace: where one tile-service project keeps everything on disk.

    root/
      datasets/      optional: your own dataset files and catalog.json targets
      data/raw/      downloaded and prepared sources       (Workspace.sources)
      public/data/   resumable builds, one dir per target  (Workspace.builds)

Dataset `sourceFile` values and catalog `raw` folders are relative to
`sources`. Datasets and targets in `root/datasets/` extend, or override by
name, the ones packaged with tile-service.
"""

from pathlib import Path

from tile_service.runtime import catalog_path, read_json


class Workspace:
    def __init__(self, root=".", *, sources=None, builds=None):
        self.root = Path(root).expanduser()
        self.sources = Path(sources) if sources else self.root / "data" / "raw"
        self.builds = Path(builds) if builds else self.root / "public" / "data"

    def __repr__(self):
        return f"Workspace({str(self.root)!r})"

    # Catalog ---------------------------------------------------------------

    def _local(self, name):
        path = self.root / "datasets" / name
        return path if path.is_file() else None

    def dataset(self, name):
        """A dataset or sites file, preferring the workspace's own copy."""
        return read_json(self._local(name) or catalog_path(name))

    def targets(self):
        """Packaged targets, extended or overridden by `root/datasets/catalog.json`."""
        targets = dict(read_json(catalog_path("catalog.json"))["targets"])
        local = self._local("catalog.json")
        if local:
            targets.update(read_json(local)["targets"])
        return targets

    def source_path(self, relative):
        """Where a source lives; absolute paths pass through."""
        return self.sources / relative

    def path(self, relative):
        """A path given relative to the workspace root; absolute paths pass through."""
        return self.root / relative

    # Operations --------------------------------------------------------------

    def status(self, name):
        from tile_service import catalog

        return catalog.status(self, name)

    def build(self, *targets, **options):
        """Build targets by name (or "all"). See `tile_service.catalog.build`."""
        from tile_service import catalog

        names = list(self.targets()) if list(targets) == ["all"] else targets
        if len(names) > 1 and (options.get("output") or options.get("source")):
            raise RuntimeError("--output and --source apply to a single target")
        for name in names:
            catalog.build(self, name, **options)

    def release(self, output="published", *, pack=True):
        """Pack every build, then export a new release directory. Returns its path."""
        from tile_service import releases

        if pack:
            releases.pack(self.builds)
        return releases.export(self.builds, self.path(output))

    def upload(self, release_dir, **options):
        """Upload an exported release to S3. See `tile_service.s3.upload`."""
        from tile_service import s3

        s3.upload(self.path(release_dir), **options)
