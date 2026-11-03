"""A fake `hyperframes render` for tests, installed as a project's `node_modules/.bin/hyperframes`
by the `project` fixture.

It prints the real CLI's `@hf-progress` lines and writes what it was given as the "video": its
flags, the variables file's contents and the environment HyperFrames reads, so a test can see the
script arrived and where the browser, ffmpeg and the cache are. Composition `broken.html` fails
like a bad render; `slow.html` pauses between frames so a poll can catch it mid-render;
`empty.html` exits 0 without writing a file; `snoop.html` is an agent's project trying to break
out of the connector sandbox (to read the data directory's keys and the world's other assets,
write outside its stage and into its own project, and reach the internet) and records what it got.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

assert sys.argv[1] == "render", sys.argv
project = sys.argv[2]
flags = dict(a[2:].split("=", 1) for a in sys.argv[3:] if a.startswith("--") and "=" in a)
composition, output = flags["composition"], flags["output"]
if composition == "broken.html":
    print('@hf-progress {"code":"compile","pct":2}')
    print("Error: composition broken.html has no root data-composition-id", flush=True)
    sys.exit(1)
print('@hf-progress {"code":"compile","pct":2}', flush=True)
for frame in (10, 20, 30):
    print(f'@hf-progress {{"code":"capture","done":{frame},"total":30,"pct":{frame * 2}}}')
    sys.stdout.flush()
    if composition == "slow.html":
        time.sleep(1.0)  # longer than a poll takes on a loaded CI runner
print('@hf-progress {"code":"encode","done":30,"total":30,"pct":90}', flush=True)
snooped: dict[str, str] = {}
if composition == "snoop.html":
    world = next(p for p in Path(output).resolve().parents if p.parent.name == "worlds")
    data = world.parent.parent
    tries = {
        "read_keys": lambda: (data / ".env").read_text(),
        "read_world": lambda: (world / "assets" / "notes.md").read_text(),
        "write_outside": lambda: (data / "planted.txt").write_text("x"),
        "write_project": lambda: (Path.cwd() / "planted.txt").write_text("x"),
        "network": lambda: urllib.request.urlopen("http://example.com/", timeout=5).read(),
    }
    for name, attempt in tries.items():
        try:
            snooped[name] = f"got {attempt()!r}"
        except OSError as exc:
            snooped[name] = f"refused: {exc}"
if composition != "empty.html":
    given = json.loads(Path(flags["variables-file"]).read_text())
    env = {k: v for k, v in os.environ.items() if "HYPERFRAMES" in k or k == "DO_NOT_TRACK"}
    seen = {"project": project, "flags": flags, "env": env, "snooped": snooped}
    Path(output).write_text(json.dumps({**seen, **given}))
