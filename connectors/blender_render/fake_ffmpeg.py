"""A fake `ffmpeg` (and `ffprobe`, which does nothing) for tests, put on PATH by the suite.

Instead of a video it writes, to the output (its last argument), JSON naming what it was given:
the frames a `frame_%04d.png` input matched, or the files a concat list names, so a test sees
which frames went into which shot and the order shots were joined in.
"""

import json
import re
import sys
from pathlib import Path

if Path(sys.argv[0]).name == "ffprobe":
    sys.exit(0)
args = sys.argv[1:]
given = Path(args[args.index("-i") + 1])
if given.suffix == ".txt":
    inputs = re.findall(r"^file '(.*)'$", given.read_text(), re.M)
else:
    inputs = sorted(p.name for p in given.parent.glob("frame_*.png"))
Path(args[-1]).write_text(json.dumps({"args": args, "inputs": inputs}))
