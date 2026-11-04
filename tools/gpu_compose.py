"""Write compose.gpu.yaml, which hands the `sandbox` service the host's NVIDIA GPU (README,
Deployment, GPU renders), from the NVIDIA Container Toolkit's CDI spec.

The spec's device files and driver files become plain `devices` and read-only `volumes`, and its
`create-symlinks` hook the driver's sonames (libEGL_nvidia.so.0) as the host's own links. Nothing
under /proc and no other hook comes along: the toolkit binds a rewritten
/proc/driver/nvidia/params over the real one, and over any mount under /proc bubblewrap cannot
mount the fresh /proc every connector's sandbox needs, so with CDI or `--gpus` no connector would
start. Run it again after a driver update, which renames the driver's files:

    python3 tools/gpu_compose.py [/var/run/cdi/nvidia.yaml] [compose.gpu.yaml]

It runs on the host's own Python (3.8 on Ubuntu 20.04) with PyYAML, as the toolkit's hosts have.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

SPEC = Path("/var/run/cdi/nvidia.yaml")
OUT = Path(__file__).resolve().parents[1] / "compose.gpu.yaml"
HEADER = """\
# Written by tools/gpu_compose.py from the NVIDIA Container Toolkit's CDI spec: the host's GPU for
# the sandbox (README, Deployment, GPU renders), nothing under /proc. Host-specific; write it
# again after a driver update.
"""


def compose(spec: dict[str, Any], exists: Callable[[str], bool] = os.path.exists) -> dict:
    """The `sandbox` service's devices and read-only bind mounts for the spec's device `all`, the
    spec-wide edits included, leaving out /proc and every hook but `create-symlinks`, whose links
    are mounted from the host's own (those that exist there, and none under /dev, whose device
    files `devices` brings). Long-form mounts, as a /dev/dri/by-path name holds colons."""
    edits = [spec.get("containerEdits") or {}]
    edits += [d.get("containerEdits") or {} for d in spec.get("devices", []) if d["name"] == "all"]
    devices = [n["path"] for e in edits for n in e.get("deviceNodes", [])]
    mounts = [(m["hostPath"], m["containerPath"]) for e in edits for m in e.get("mounts", [])]
    for e in edits:
        for hook in e.get("hooks", []):
            if "create-symlinks" not in hook.get("args", []):
                continue
            links = [a.split("::")[1] for a in hook["args"] if "::" in a]
            mounts += [(link, link) for link in links if exists(link)]
    skip = ("/proc", "/dev")
    kept = dict.fromkeys(
        (h, c) for h, c in mounts if not any(c == s or c.startswith(s + "/") for s in skip)
    )
    volumes = [{"type": "bind", "source": h, "target": c, "read_only": True} for h, c in kept]
    return {"services": {"sandbox": {"devices": list(dict.fromkeys(devices)), "volumes": volumes}}}


def main(spec: Path = SPEC, out: Path = OUT) -> None:
    written = compose(yaml.safe_load(spec.read_text()))
    out.write_text(HEADER + yaml.safe_dump(written, sort_keys=False))
    sandbox = written["services"]["sandbox"]
    print(f"{out}: {len(sandbox['devices'])} devices, {len(sandbox['volumes'])} files")


if __name__ == "__main__":
    main(*map(Path, sys.argv[1:3]))
