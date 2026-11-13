"""Build records: which build an output directory belongs to.

A build's identity names only what determines its output bytes: source
content hash, geometry, levels, encoding, toolchain. Descriptive provenance
(titles, credits, pages, product names) is not identity; builders republish it
on every run without touching tiles.
"""

from pathlib import Path

from tile_service.runtime import atomic_json, read_json


class BuildRecord:
    """Claim `path` for `identity`, or refuse.

    A fresh or empty directory adopts the identity. The same identity resumes.
    Anything else is refused, so settings never mix in one directory.
    """

    def __init__(self, path, identity, *, state="build-state.json"):
        path = Path(path)
        record = path / state
        if record.exists():
            if read_json(record) != identity:
                raise RuntimeError(
                    "Output belongs to another source/settings/toolchain. "
                    f"Choose a new output directory: {path}"
                )
        elif path.exists() and any(path.iterdir()):
            raise RuntimeError(f"Nonempty output without {state}; choose a new directory: {path}")
        else:
            atomic_json(record, identity)
