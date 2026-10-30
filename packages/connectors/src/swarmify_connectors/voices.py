"""Starter voices for a world, recorded from no one: `mise run voices:seed -- <world id>
[presets...]` has Kokoro speak one neutral sentence in each preset, saves each take as a
reference clip in the world's `assets/voices/`, and writes its catalog entry (README, Voices).

Each entry carries the clip, which Chatterbox clones, and the preset itself, which Kokoro speaks
directly, so a fallback from one engine to the other keeps the same voice. Part of the speech
connector: only it gives `voices/` a meaning.

The operator's edits to the catalog live here too (`listed`, `add`, `edit`, `remove`), so the
Assets page writes voices.json exactly as `voices:seed` does and the connector reads it unchanged.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from swarmify_connectors import speech
from swarmify_connectors.speech import _ID, VOICES, decode

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


CLIP_SECONDS = (5.0, 10.0)
"""The length of a reference clip Chatterbox clones from (README, Voices)."""
_lock = threading.Lock()
# ponytail: one lock for every world's catalog in this process; per-world locks if it contends.


def clip_seconds(wav: bytes) -> float:
    """A clip's length. Raises ValueError for one the speech connector cannot send: not a
    16-bit WAV, or outside CLIP_SECONDS."""
    try:
        samples, rate = decode(wav)
    except ValueError as exc:
        raise ValueError(f"the clip must be a 16-bit WAV file: {exc}") from exc
    seconds = len(samples) / rate
    low, high = CLIP_SECONDS
    if not low <= seconds <= high:
        raise ValueError(
            f"the clip is {seconds:.1f} s; Chatterbox clones from {low:g} to {high:g} s of speech"
        )
    return seconds


def listed(assets: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The catalog as the speech connector reads it (`speech.catalog`), each voice with its
    clip's file name and length in seconds (None without a clip or for one it cannot read)."""
    found, warnings = speech.catalog(str(assets))
    for voice in found:
        clip = voice["clip"] and Path(voice["clip"])
        voice["clip"], voice["seconds"] = (clip.name, _length(clip)) if clip else (None, None)
    return found, warnings


def _length(clip: Path) -> float | None:
    try:
        samples, rate = decode(clip.read_bytes())
    except (OSError, ValueError):
        return None
    return round(len(samples) / rate, 1)


def _entries(folder: Path) -> list[Any]:
    file = folder / "voices.json"
    if not file.is_file():
        return []
    entries = json.loads(file.read_text())  # a ValueError when broken: never overwrite it
    if not isinstance(entries, list):
        raise ValueError("voices.json is not a list of voices; fix it by hand first")
    return entries


def _write(folder: Path, entries: list[Any]) -> None:
    """voices.json replaced in one step, so a reader never sees half of it."""
    tmp = folder / ".voices.json.tmp"
    tmp.write_text(json.dumps(entries, indent=2) + "\n")
    os.replace(tmp, folder / "voices.json")


def _index(entries: list[Any], name: str) -> int:
    for i, e in enumerate(entries):
        if isinstance(e, dict) and e.get("name") == name:
            return i
    raise LookupError(f"no voice named {name}")


def _told(description: str, tags: list[str]) -> tuple[str, list[str]]:
    description = description.strip()
    if not description:
        raise ValueError("a voice needs a description: what it sounds like and what it fits")
    return description, list(dict.fromkeys(t.strip() for t in tags if t.strip()))


def add(
    folder: Path,
    name: str,
    description: str,
    tags: list[str],
    consent: str,
    clip: bytes,
    engine: str = "chatterbox",
) -> dict[str, Any]:
    """Save `clip` as `<name>.wav` in `folder` (a world's `assets/voices/`) and append its
    entry to voices.json. Raises ValueError naming what is wrong."""
    if not _ID.match(name):
        raise ValueError(f"a voice name is letters, digits, _ or -, got {name!r}")
    if not consent.strip():
        raise ValueError("a voice needs consent: your own voice, or the speaker's permission")
    description, tags = _told(description, tags)
    clip_seconds(clip)
    entry = {"name": name, "description": description, "tags": tags, "engine": engine,
             "clip": f"{name}.wav", "consent": consent.strip()}  # fmt: skip
    with _lock:
        folder.mkdir(parents=True, exist_ok=True)
        entries = _entries(folder)
        try:
            _index(entries, name)
        except LookupError:
            pass
        else:
            raise ValueError(f"a voice is already named {name}")
        (folder / entry["clip"]).write_bytes(clip)
        _write(folder, [*entries, entry])
    return entry


def edit(folder: Path, name: str, description: str, tags: list[str]) -> dict[str, Any]:
    """Change a voice's description and tags, keeping the rest of its entry. Raises
    LookupError when there is no such voice, ValueError for an empty description."""
    description, tags = _told(description, tags)
    with _lock:
        entries = _entries(folder)
        i = _index(entries, name)
        entries[i] = {**entries[i], "description": description, "tags": tags}
        _write(folder, entries)
    return entries[i]


def remove(folder: Path, name: str) -> None:
    """Drop a voice's entry and its clip, unless another entry uses the same clip. Raises
    LookupError when there is no such voice."""
    with _lock:
        entries = _entries(folder)
        gone = entries.pop(_index(entries, name))
        _write(folder, entries)
        clip = gone.get("clip")
        others = {e.get("clip") for e in entries if isinstance(e, dict)}
        if isinstance(clip, str) and Path(clip).name == clip and clip not in others:
            (folder / clip).unlink(missing_ok=True)


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
