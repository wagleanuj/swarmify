"""The pager's long-lived process: records its environment, then waits to be stopped."""

import json
import os
import time

keep = ("PAGER_TOKEN", "PAGER_ROOM", "SWARMIFY_MCP_URL", "SWARMIFY_API_TOKEN")
with open("listened.json", "a") as out:
    out.write(json.dumps({k: os.environ.get(k) for k in keep}) + "\n")
while True:
    time.sleep(1)
