"""The pager's long-lived process: records its environment in its HOME, the one folder its
sandbox lets it write, then waits to be stopped."""

import json
import os
import time
from pathlib import Path

keep = ("PAGER_TOKEN", "PAGER_ROOM", "SWARMIFY_MCP_URL", "SWARMIFY_API_TOKEN")
with open(Path(os.environ["HOME"]) / "listened.json", "a") as out:
    out.write(json.dumps({k: os.environ.get(k) for k in keep}) + "\n")
while True:
    time.sleep(1)
