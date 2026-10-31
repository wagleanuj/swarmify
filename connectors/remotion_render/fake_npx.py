"""A fake `remotion render` for tests, installed as a project's `node_modules/.bin/remotion` by
the `fake_npx` fixture.

It prints the real CLI's progress lines and writes the props it was given as the "video", so a
test can see the script arrived. Composition `Broken` fails like a bad render; `Slow` pauses
between frames so a poll can catch it mid-render; `Empty` exits 0 without writing a file;
`Snoop` is an agent's project trying to break out of the connector sandbox (to read the data
directory's keys, write outside its stage and reach the internet) and records what it got.
Like the real CLI it serves the bundle on `--port` (3000 without one) and, when another server
already answers there, talks to that one and fails.
"""

import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("-")]
assert args[:1] == ["render"], args
_, entry, composition, output = args[:4]
props = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--props="))
browser = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--browser-executable=")), "")
concurrency = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--concurrency=")), "")
port = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--port=")), "3000"))
try:
    socket.create_connection(("localhost", port), timeout=1).close()
except OSError:
    pass
else:
    print("Error: window.getStaticCompositions is not defined", flush=True)
    sys.exit(1)
if composition == "Broken":
    print("Bundling 100%")
    print(f"Error: No composition with the ID {composition} found.", flush=True)
    sys.exit(1)
for frame in range(1, 4):
    print(f"Rendered {frame}/3, time remaining: 0s", flush=True)
    if composition == "Slow":
        time.sleep(1.0)  # longer than a poll takes on a loaded CI runner
print("Encoded 3/3", flush=True)
snooped: dict[str, str] = {}
if composition == "Snoop":
    data = next(p.parent for p in Path(output).resolve().parents if p.name == "worlds")
    tries = {
        "read_keys": lambda: (data / ".env").read_text(),
        "write_outside": lambda: (data / "planted.txt").write_text("x"),
        "network": lambda: urllib.request.urlopen("http://example.com/", timeout=5).read(),
    }
    for name, attempt in tries.items():
        try:
            snooped[name] = f"got {attempt()!r}"
        except OSError as exc:
            snooped[name] = f"refused: {exc}"
if composition != "Empty":
    given = json.loads(Path(props).read_text())
    Path(output).write_text(
        json.dumps(
            {
                "entry": entry,
                "composition": composition,
                "browser": browser,
                "concurrency": concurrency,
                "snooped": snooped,
                **given,
            }
        )
    )
