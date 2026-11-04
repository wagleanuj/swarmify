"""tools/gpu_compose.py: the sandbox gets the GPU from the CDI spec, never a mount under /proc."""

from pathlib import Path

import yaml

from tools import gpu_compose

LIB = "/usr/lib/x86_64-linux-gnu"
SPEC = {
    "cdiVersion": "0.5.0",
    "kind": "nvidia.com/gpu",
    "devices": [
        {"name": "0", "containerEdits": {"deviceNodes": [{"path": "/dev/nvidia9"}]}},
        {
            "name": "all",
            "containerEdits": {
                "deviceNodes": [{"path": "/dev/nvidia0"}, {"path": "/dev/dri/renderD128"}],
                "mounts": [{"hostPath": "/proc/x", "containerPath": "/proc/driver/nvidia/x"}],
            },
        },
    ],
    "containerEdits": {
        "deviceNodes": [{"path": "/dev/nvidiactl"}],
        "mounts": [
            {
                "hostPath": f"{LIB}/libEGL_nvidia.so.560",
                "containerPath": f"{LIB}/libEGL_nvidia.so.560",
            },
            {
                "hostPath": "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
                "containerPath": "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
            },
        ],
        "hooks": [
            {
                "hookName": "createContainer",
                "path": "/usr/bin/nvidia-cdi-hook",
                "args": [
                    "nvidia-cdi-hook",
                    "create-symlinks",
                    "--link",
                    f"libEGL_nvidia.so.560::{LIB}/libEGL_nvidia.so.0",
                    "--link",
                    f"libcuda.so.560::{LIB}/libcuda.so.1",
                    "--link",
                    "../card0::/dev/dri/by-path/pci-0000:01:00.0-card",
                ],
            },
            {
                "hookName": "createContainer",
                "path": "/usr/bin/nvidia-cdi-hook",
                "args": ["nvidia-cdi-hook", "disable-device-node-modification"],
            },
        ],
    },
}


def test_the_sandbox_gets_the_devices_files_and_sonames_and_nothing_under_proc() -> None:
    """The toolkit's hooks bind a rewritten /proc/driver/nvidia/params, over which bubblewrap
    cannot mount a fresh /proc: no connector would start (seen on the box, 2026-11-03)."""
    on_host = {f"{LIB}/libEGL_nvidia.so.0", "/dev/dri/by-path/pci-0000:01:00.0-card"}
    sandbox = gpu_compose.compose(SPEC, on_host.__contains__)["services"]["sandbox"]
    assert sandbox["devices"] == ["/dev/nvidiactl", "/dev/nvidia0", "/dev/dri/renderD128"]
    vendor = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
    files = [f"{LIB}/libEGL_nvidia.so.560", vendor, f"{LIB}/libEGL_nvidia.so.0"]  # no libcuda link
    assert sandbox["volumes"] == [
        {"type": "bind", "source": f, "target": f, "read_only": True} for f in files
    ]  # nothing under /proc, nor /dev, whose by-path names hold colons


def test_main_writes_a_compose_file_saying_where_it_came_from(tmp_path: Path) -> None:
    spec, out = tmp_path / "nvidia.yaml", tmp_path / "compose.gpu.yaml"
    spec.write_text(yaml.safe_dump(SPEC))
    gpu_compose.main(spec, out)
    text = out.read_text()
    assert text.startswith("# Written by tools/gpu_compose.py")
    assert yaml.safe_load(text)["services"]["sandbox"]["devices"][0] == "/dev/nvidiactl"
