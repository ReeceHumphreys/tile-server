"""Build planetary imagery and elevation as cube-sphere tiles, released as PMTiles.

from tile_service import Workspace

ws = Workspace("~/planet-data")   # datasets/, data/raw/, public/data/
ws.targets()                      # what can be built
ws.build("moon/wac", "moon/elevation-tiles")
ws.release("releases/v3")
ws.upload("releases/v3", bucket="planet-tiles", release_id="v3")
"""

from tile_service.workspace import Workspace

__all__ = ["Workspace"]
