"""A fake `blender` for tests, put on PATH as `blender` by the suite.

Called as the render calls Blender (`-b --factory-startup -noaudio --python-exit-code 1 --python
<script> -- <quality>`), it reads the shot script for what to do, from comment lines:
`# frames: N` (3 by default) frames to write, `# fail` raises like a broken script, `# empty`
exits cleanly with no frames, `# slow` takes 0.3 s a frame. It writes `frame_NNNN.png` at the
project's width and height into BLENDER_SHOT_OUT, every frame or those BLENDER_SHOT_FRAMES lists
up to its last, and appends a record of each run (its arguments, the environment it read, when it
started and ended, whether it could read the world's Assets) to `calls.jsonl` beside that folder.
"""

import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path


def png(path: Path, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    head = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


args = sys.argv[1:]
assert args[:6] == ["-b", "--factory-startup", "-noaudio", "--python-exit-code", "1", "--python"]
script, quality = Path(args[6]), args[8]
assert args[7] == "--", args
said = script.read_text()
project = Path(os.environ["BLENDER_PROJECT"])
spec = json.loads((project / "render.json").read_text())
out = Path(os.environ["BLENDER_SHOT_OUT"])
listed = os.environ["BLENDER_SHOT_FRAMES"]
count = next(
    (int(line.split(":")[1]) for line in said.splitlines() if line.startswith("# frames:")), 3
)
frames = [int(f) for f in listed.split(",") if int(f) <= count] if listed else range(1, count + 1)
env = {k: v for k, v in os.environ.items() if k.startswith(("BLENDER_", "__GL_"))}
assets = Path(os.environ["BLENDER_ASSETS"]) if os.environ["BLENDER_ASSETS"] else None
record = {"args": args, "env": env, "pid": os.getpid(), "start": time.time()}
if assets:
    try:
        record["assets"] = sorted(p.name for p in assets.iterdir())
    except OSError as exc:
        record["assets"] = f"refused: {exc}"
calls = out.parent / "calls.jsonl"
with open(calls, "a") as f:
    f.write(json.dumps({**record, "event": "start"}) + "\n")
print(f"Blender 5.2.1 (fake), shot {os.environ['BLENDER_SHOT']}, quality {quality}", flush=True)
if "# fail" in said:
    print("Traceback (most recent call last):\nRuntimeError: boom in the shot script", flush=True)
    sys.exit(1)
if "# empty" not in said:
    for n in frames:
        if "# slow" in said:
            time.sleep(0.3)
        print(f"Fra:{n} Mem:12.00M | Rendering 1 / 64 samples", flush=True)
        png(out / f"frame_{n:04d}.png", spec["width"], spec["height"], (n * 20 % 256, 80, 160))
        print(f"Saved: '{out / f'frame_{n:04d}.png'}'", flush=True)
with open(calls, "a") as f:
    f.write(json.dumps({**record, "event": "end", "end": time.time()}) + "\n")
