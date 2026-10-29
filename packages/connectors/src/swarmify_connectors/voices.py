"""Starter voices for a world, recorded from no one: `mise run voices:seed -- <world id>
[presets...]` has Kokoro speak one neutral sentence in each preset, saves each take as a
reference clip in the world's `assets/voices/`, and writes its catalog entry (README, Voices).

Each entry carries the clip, which Chatterbox clones, and the preset itself, which Kokoro speaks
directly, so a fallback from one engine to the other keeps the same voice. Part of the speech
connector: only it gives `voices/` a meaning.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from swarmify_connectors import speech
from swarmify_connectors.speech import VOICES, decode

KOKORO = "http://localhost:8880"
SENTENCE = (
    "On a quiet morning the town wakes slowly, the bakery opens its doors, and people stop to"
    " talk about the weather before they walk on to work."
)
"""About nine seconds in most presets: Chatterbox clones from five to ten."""
STARTERS: dict[str, tuple[str, list[str]]] = {
    "af_heart": ("Warm, friendly American woman", ["warm", "friendly", "female", "american"]),
    "am_michael": ("Warm, steady American man", ["warm", "steady", "male", "american"]),
    "af_bella": ("Bright, lively American woman", ["bright", "lively", "female", "american"]),
    "am_puck": ("Bright, playful American man", ["bright", "playful", "male", "american"]),
    "am_onyx": ("Deep, resonant American man", ["deep", "resonant", "male", "american"]),
    "am_fenrir": ("Deep, strong American man", ["deep", "strong", "male", "american"]),
    "bf_emma": ("Clear, composed British woman", ["british", "clear", "female"]),
    "bm_fable": ("Calm British storyteller", ["calm", "narrator", "british", "male"]),
}
"""The default set and how each is described: two warm, two bright, two deep, one British and a
calm narrator."""


def described(preset: str) -> tuple[str, list[str]]:
    """A preset's description and tags; one not in STARTERS is told by its name, as Kokoro's
    names say accent and sex (`bf_` a British woman)."""
    if preset in STARTERS:
        return STARTERS[preset]
    accent = {"a": "american", "b": "british"}.get(preset[:1])
    sex = {"f": "female", "m": "male"}.get(preset[1:2])
    tags = [t for t in (accent, sex) if t]
    return f"Kokoro preset {preset}", tags


def seed(http: httpx.Client, base: str, voices: Path, presets: list[str]) -> list[str]:
    """Speak SENTENCE in each preset the engine at `base` lists, save `<preset>.wav` in `voices`
    and add or replace its entry in voices.json, keeping the others. Returns what was skipped."""
    listed = speech.voice_names(http.get(f"{base}/v1/audio/voices").json().get("voices"))
    voices.mkdir(parents=True, exist_ok=True)
    file = voices / "voices.json"
    entries = json.loads(file.read_text()) if file.is_file() else []
    skipped = []
    for preset in presets:
        if preset not in listed:
            skipped.append(f"{preset}: not among the engine's voices")
            continue
        body = {"model": "kokoro", "input": SENTENCE, "voice": preset, "response_format": "wav"}
        r = http.post(f"{base}/v1/audio/speech", json=body)
        r.raise_for_status()
        samples, rate = decode(r.content)
        if len(samples) / rate < 5:
            skipped.append(f"{preset}: the clip is under 5 s, too short to clone")
            continue
        (voices / f"{preset}.wav").write_bytes(r.content)
        description, tags = described(preset)
        entry = {
            "name": preset,
            "description": description,
            "tags": tags,
            "engine": "chatterbox",
            "clip": f"{preset}.wav",
            "preset": preset,
            "consent": f"synthetic: Kokoro preset {preset}, Apache 2.0",
        }
        entries = [e for e in entries if not (isinstance(e, dict) and e.get("name") == preset)]
        entries.append(entry)
    file.write_text(json.dumps(entries, indent=2) + "\n")
    return skipped


def _up(http: httpx.Client, base: str) -> bool:
    try:
        return http.get(f"{base}/health").is_success
    except httpx.HTTPError:
        return False


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: mise run voices:seed -- <world id> [kokoro presets...]", file=sys.stderr)
        return 2
    world, presets = argv[0], argv[1:] or list(STARTERS)
    data = Path(os.environ.get("SWARMIFY_DATA_DIR", Path.home() / ".swarmify"))
    folder = data / "worlds" / world
    if not folder.is_dir():
        print(f"no world {world} in {data / 'worlds'}", file=sys.stderr)
        return 1
    base = os.environ.get("KOKORO_URL", KOKORO).rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(300, connect=5)) as http:
        if not _up(http, base):
            docker = shutil.which("docker")
            if not docker or subprocess.run([docker, "info"], capture_output=True).returncode:
                print(
                    f"Kokoro is not up at {base} and Docker is unavailable: start Kokoro"
                    " (README, Speech engines) and run this again",
                    file=sys.stderr,
                )
                return 1
            print("starting Kokoro: docker compose up -d kokoro")
            subprocess.run(["docker", "compose", "up", "-d", "kokoro"], check=True)
            deadline = time.monotonic() + 180
            while not _up(http, base):
                if time.monotonic() > deadline:
                    print(f"Kokoro did not come up at {base}", file=sys.stderr)
                    return 1
                time.sleep(2)
        skipped = seed(http, base, folder / "assets" / VOICES, presets)
    for line in skipped:
        print(f"skipped {line}", file=sys.stderr)
    print(f"{len(presets) - len(skipped)} voices in {folder / 'assets' / VOICES / 'voices.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
