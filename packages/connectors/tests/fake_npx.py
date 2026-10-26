"""A fake `npx remotion render` for tests, installed as `npx` on PATH by the `fake_npx` fixture.

It prints the real CLI's progress lines and writes the props it was given as the "video", so a
test can see the script arrived. Composition `Broken` fails like a bad render; `Slow` pauses
between frames so a poll can catch it mid-render; `Empty` exits 0 without writing a file.
"""

import json
import sys
import time
from pathlib import Path

args = [a for a in sys.argv[1:] if not a.startswith("-")]
assert args[:2] == ["remotion", "render"], args
_, _, entry, composition, output = args[:5]
props = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--props="))
if composition == "Broken":
    print("Bundling 100%")
    print(f"Error: No composition with the ID {composition} found.", flush=True)
    sys.exit(1)
for frame in range(1, 4):
    print(f"Rendered {frame}/3, time remaining: 0s", flush=True)
    if composition == "Slow":
        time.sleep(0.4)
print("Encoded 3/3", flush=True)
if composition != "Empty":
    given = json.loads(Path(props).read_text())
    Path(output).write_text(json.dumps({"entry": entry, "composition": composition, **given}))
