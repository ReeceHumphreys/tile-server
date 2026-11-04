"""Build records: which build an output directory belongs to.

A build's identity names only what determines its output bytes: source
content hash, geometry, levels, encoding, toolchain. Descriptive provenance
(titles, credits, pages, product names) is not identity; builders republish it
on every run without touching tiles. Older record formats map onto the
current one through explicit upgrade rules, so finished builds stay resumable
across releases of this tool and across checkouts.
"""

from pathlib import Path

from tile_service.runtime import atomic_json, read_json


class BuildRecord:
    """Claim `path` for `identity`, or refuse.

    A fresh or empty directory adopts the identity. The same identity resumes.
    An older record resumes when one of `upgrades` maps it onto `identity`;
    `previous` then holds the old record. Anything else is refused, so
    settings never mix in one directory.
    """

    def __init__(self, path, identity, *, state="build-state.json", upgrades=()):
        self.path = Path(path)
        self.state = self.path / state
        self.identity = identity
        self.previous = None
        if self.state.exists():
            old = read_json(self.state)
            if old != identity:
                if not any(upgrade(old) == identity for upgrade in upgrades):
                    raise RuntimeError(
                        "Output belongs to another source/settings/toolchain. "
                        f"Choose a new output directory: {self.path}"
                    )
                self.previous = old
        elif self.path.exists() and any(self.path.iterdir()):
            raise RuntimeError(
                f"Nonempty output without {state}; choose a new directory: {self.path}"
            )
        else:
            atomic_json(self.state, identity)


def pick(record, keys):
    """The subset of an old record a current identity shares with it."""
    return {key: record.get(key) for key in keys}
