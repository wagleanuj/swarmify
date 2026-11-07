"""The install step's downloads (ADR 0026), run as `npm run fetch` from the package folder: Blender
and ffmpeg, each checked against a pinned SHA-256, into `vendor/`.

- Blender: the Linux x64 tarball of every version in BLENDER from download.blender.org (its
  `.sha256` file beside it gave the pin), unpacked to `vendor/blender/<version>/`. Elsewhere there
  is none to unpack, and a render uses a `blender` on PATH. `python fetch.py <version>` fetches one
  version, refused without a pin.
- ffmpeg and ffprobe, into `vendor/bin/`, with the drawtext, ass and subtitles filters and libx264
  a finish step is promised: on Linux x64 from BtbN's FFmpeg-Builds GPL tarball (ffmpeg-static's
  b6.1.1 there has no drawtext, which ffmpeg 6.1 builds only with harfbuzz), elsewhere
  ffmpeg-static's release builds, the ones hyperframes_render fetches (ffmpeg.mjs there).
"""

import gzip
import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VENDOR = Path(__file__).parent / "vendor"
BLENDER = {
    "5.2.1": "a31f524fa99a527d3d52b7f5aaa68c34e1a19d5a1c9473f79c5cc610fd5b10e9",
}
"""The Linux x64 tarball's SHA-256 of each Blender version a render may run."""
BLENDER_URL = "https://download.blender.org/release/Blender{minor}/blender-{v}-linux-x64.tar.xz"
FFMPEG_URL = "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1"
FFMPEG = {
    "ffmpeg-darwin-arm64": "8923876afa8db5585022d7860ec7e589af192f441c56793971276d450ed3bbfa",
    "ffmpeg-darwin-x64": "929b375c1182d956c51f7ac25e0b2b0411fb01f6f407aa15c9758efeb4242106",
    "ffmpeg-linux-arm64": "754a678672298bc68156adff58aa7385a592c2b30b1d0ae8750c45c915c4bac0",
    "ffprobe-darwin-arm64": "d986a8ec7b030899fe66a8a288ed809a3543338705a3ce178cfb85869c5d80be",
    "ffprobe-darwin-x64": "d4da574d6e2e197bd259b47d69cf262df9e312af24ad960444f6d806d3d4c186",
    "ffprobe-linux-arm64": "2ab6aba60ee84412dff9188720703376cb4e7aaf7e0b5e43aa8249f2acae5bf8",
}
FFMPEG_LINUX_X64 = (
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/"
    "ffmpeg-n8.1.3-27-g8dca1586f4-linux64-gpl-8.1.tar.xz",
    "41e9c565594c390622cf968d82fa4135d9e9a11d06c1515644288cbc844e3380",
)
"""ffmpeg 8.1.3, a month-end build BtbN keeps (its dailies are deleted), and its SHA-256."""
ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def download(url: str, sha256: str, to: Path) -> None:
    """`url` streamed to `to`, refused (and removed) unless it has SHA-256 `sha256`."""
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as got, open(to, "wb") as out:
        while chunk := got.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != sha256:
        to.unlink()
        raise SystemExit(f"{url} has SHA-256 {digest.hexdigest()}, not the pinned {sha256}")


def blender(version: str) -> None:
    if version not in BLENDER:
        pinned = ", ".join(BLENDER)
        raise SystemExit(f"Blender {version} has no pinned checksum here; pinned: {pinned}")
    target = VENDOR / "blender" / version
    if (target / "blender").is_file():
        return
    url = BLENDER_URL.format(minor=version.rsplit(".", 1)[0], v=version)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        tarball = Path(tmp) / "blender.tar.xz"
        print(f"fetching {url}", flush=True)
        download(url, BLENDER[version], tarball)
        with tarfile.open(tarball) as tar:
            tar.extractall(tmp, filter="data")
        (unpacked,) = [p for p in Path(tmp).iterdir() if p.is_dir()]
        unpacked.rename(target)


def ffmpeg() -> None:
    system, arch = sys.platform, ARCH.get(platform.machine().lower(), platform.machine())
    (VENDOR / "bin").mkdir(parents=True, exist_ok=True)
    if (system, arch) == ("linux", "x64"):
        url, sha256 = FFMPEG_LINUX_X64
        with tempfile.TemporaryDirectory(dir=VENDOR) as tmp:
            tarball = Path(tmp) / "ffmpeg.tar.xz"
            download(url, sha256, tarball)
            with tarfile.open(tarball) as tar:
                wanted = [m for m in tar if m.name.endswith(("/bin/ffmpeg", "/bin/ffprobe"))]
                tar.extractall(tmp, members=wanted, filter="data")
            for member in wanted:
                os.replace(Path(tmp) / member.name, VENDOR / "bin" / Path(member.name).name)
        return
    for tool in ("ffmpeg", "ffprobe"):
        name = f"{tool}-{system}-{arch}"
        if name not in FFMPEG:
            raise SystemExit(f"no {tool} build for {system} {arch}")
        gz = VENDOR / "bin" / f"{tool}.gz"
        download(f"{FFMPEG_URL}/{name}.gz", FFMPEG[name], gz)
        with gzip.open(gz) as packed, open(VENDOR / "bin" / tool, "wb") as out:
            shutil.copyfileobj(packed, out)
        gz.unlink()
        os.chmod(VENDOR / "bin" / tool, 0o755)


def main(versions: list[str]) -> None:
    for version in versions:  # refused before any download
        if version not in BLENDER:
            blender(version)
    ffmpeg()
    if sys.platform != "linux" or ARCH.get(platform.machine().lower()) != "x64":
        print("no Blender build for this platform: put blender on PATH")
        return
    for version in versions or list(BLENDER):
        blender(version)


if __name__ == "__main__":
    main(sys.argv[1:])
