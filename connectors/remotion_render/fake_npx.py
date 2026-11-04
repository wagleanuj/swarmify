"""A fake `remotion render` for tests, installed as a project's `node_modules/.bin/remotion` by
the `fake_npx` fixture.

It prints the real CLI's progress lines and writes the props it was given as the "video", so a
test can see the script arrived. Composition `Broken` fails like a bad render; `Slow` pauses
between frames so a poll can catch it mid-render; `Empty` exits 0 without writing a file;
`Snoop` is an agent's project trying to break out of the connector sandbox (to read the data
directory's keys and the world's other assets, write outside its stage and into its own project,
and reach the internet) and records what it got.
Like the real CLI it serves the bundle on `--port` (3000 without one) and, when another server
already answers there, talks to that one and fails.

It prints its arguments first (`fake remotion: [...]`), which the job's log keeps, and fakes the
other commands a GPU render runs: `bundle` writes an index.html, `compositions` lists every
composition at 1500 frames, a render of `--frames=a-b` prints that many frames and writes its
`--separate-audio-to` too (`Broken` fails only on a range holding frame 900), and `ffmpeg` writes
the output naming the parts and audio its concat lists joined.
"""

import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

print(f"fake remotion: {json.dumps(sys.argv[1:])}", flush=True)
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if args[:1] == ["bundle"]:
    out = Path(next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--out-dir=")))
    out.mkdir(parents=True)
    (out / "index.html").write_text("<html></html>")
    print("Bundling 100%", flush=True)
    sys.exit(0)
if args[:1] == ["compositions"]:
    assert (Path(args[1]) / "index.html").is_file(), args
    print("The following compositions are available:\n")
    for name in ("Short", "Broken", "Empty"):
        print(f"{name:<20}30      1080x1920      1500 (50.00 sec)")
    sys.exit(0)
if args[:1] == ["ffmpeg"]:
    lists = [Path(sys.argv[i + 1]) for i, a in enumerate(sys.argv) if a == "-i"]
    joined = [
        [
            json.loads((each.parent / line[6:-1]).read_text())
            for line in each.read_text().splitlines()
        ]
        for each in lists
    ]
    Path(args[-1]).write_text(json.dumps({"parts": joined[0], "audio": joined[1]}))
    sys.exit(0)
assert args[:1] == ["render"], args
_, entry, composition, output = args[:4]
props = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--props="))
browser = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--browser-executable=")), "")
concurrency = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--concurrency=")), "")
frames = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--frames=")), "")
audio = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--separate-audio-to=")), "")
first, _, last = frames.partition("-")
count = int(last) - int(first) + 1 if last else 3
port = int(next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--port=")), "3000"))
try:
    socket.create_connection(("localhost", port), timeout=1).close()
except OSError:
    pass
else:
    print("Error: window.getStaticCompositions is not defined", flush=True)
    sys.exit(1)
if composition == "Broken" and (not last or int(first) <= 900 <= int(last)):
    print("Bundling 100%")
    print(f"Error: No composition with the ID {composition} found.", flush=True)
    sys.exit(1)
for frame in range(1, count + 1):
    print(f"Rendered {frame}/{count}, time remaining: 0s", flush=True)
    if composition == "Slow":
        time.sleep(1.0)  # longer than a poll takes on a loaded CI runner
print(f"Encoded {count}/{count}", flush=True)
if audio:
    Path(audio).write_text(json.dumps({"audio": frames}))
snooped: dict[str, str] = {}
if composition == "Snoop":
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
if composition != "Empty":
    given = json.loads(Path(props).read_text())
    Path(output).write_text(
        json.dumps(
            {
                "entry": entry,
                "composition": composition,
                "browser": browser,
                "concurrency": concurrency,
                "frames": frames,
                "snooped": snooped,
                **given,
            }
        )
    )
