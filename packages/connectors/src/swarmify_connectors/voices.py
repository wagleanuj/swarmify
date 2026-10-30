"""A world's voice catalog as the Assets page edits it: `assets/voices/voices.json` and its clips,
read with the speech package's rules and written as its `voices.py` seeding writes them (`listed`,
`add`, `edit`, `remove`).

The speech connector is a Connector Package now (connectors/speech) and its own copy of these
rules is what voices a script; this one only serves the core Voices API.
"""

# ponytail: a copy of the speech package's catalog rules; #220 moves voices into the package's
# Owned Folder and deletes this module with the core Voices API.

import io
import json
import os
import re
import sys
import threading
import wave
from array import array
from pathlib import Path
from typing import Any

VOICES = "voices"
"""The folder of a world's assets holding its voice catalog, voices.json, and its clips."""
_ID = re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def catalog(assets_path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The world's voices from `<assets>/voices/voices.json`, a list of `{name, description,
    tags, engine, clip?, preset?, consent}` (`clip` made a path), and a warning for each entry
    ignored: one without consent, without a clip in that folder or a preset, or named twice."""
    folder = Path(assets_path) / VOICES
    file = folder / "voices.json"
    if not assets_path or not file.is_file():
        return [], []
    try:
        raw = json.loads(file.read_text())
    except ValueError as exc:
        return [], [f"voices.json is not JSON, so no voice of it is used: {exc}"]
    if not isinstance(raw, list):
        return [], ["voices.json is not a list of voices, so none of it is used"]
    voices: list[dict[str, Any]] = []
    warnings = []
    for n, entry in enumerate(raw, 1):
        if problem := _unusable(entry, folder, voices):
            warnings.append(f"voice {n} in voices.json is ignored: {problem}")
            continue
        clip = entry.get("clip")
        voices.append(
            {
                **{k: entry[k].strip() for k in ("name", "description", "engine", "consent")},
                "tags": entry.get("tags") or [],
                "clip": str(folder / clip) if clip else None,
                "preset": entry.get("preset") or None,
            }
        )
    return voices, warnings


def _unusable(entry: Any, folder: Path, kept: list[dict[str, Any]]) -> str:
    def text(key: str) -> bool:
        return isinstance(entry.get(key), str) and bool(entry[key].strip())

    if not isinstance(entry, dict):
        return "it is not an object"
    name = entry.get("name")
    if not isinstance(name, str) or not _ID.match(name):
        return f"a name is letters, digits, _ or -, got {name!r}"
    if any(v["name"] == name for v in kept):
        return f"{name} is named twice"
    if not text("consent"):
        return f"{name} has no consent, the short statement of the right to use the voice"
    if missing := [k for k in ("description", "engine") if not text(k)]:
        return f"{name} has no {missing[0]}"
    tags = entry.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return f"{name}'s tags are not a list of words"
    clip, preset = entry.get("clip"), entry.get("preset")
    if clip and (not isinstance(clip, str) or Path(clip).name != clip or clip.startswith(".")):
        return f"{name}'s clip is not a file name in voices/: {clip!r}"
    if clip and not (folder / clip).is_file():
        return f"{name}'s clip {clip} is not in voices/"
    if preset is not None and not text("preset"):
        return f"{name}'s preset is not an engine voice name"
    return "" if clip or preset else f"{name} has neither a clip nor a preset"


def decode(wav: bytes) -> tuple[array, int]:
    """The first channel's 16-bit samples and the rate. Counts the frames itself: a streamed
    WAV (Kokoro's) declares a bogus length. Raises ValueError."""
    try:
        with wave.open(io.BytesIO(wav)) as w:
            if w.getsampwidth() != 2:
                raise ValueError(f"takes must be 16-bit WAV, got {8 * w.getsampwidth()}-bit")
            channels, rate, data = w.getnchannels(), w.getframerate(), w.readframes(2**31 - 1)
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"the take is not a WAV file: {exc}") from exc
    samples = array("h", data[: len(data) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return samples[::channels], rate


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
    """The catalog as the speech connector reads it (`catalog`), each voice with its
    clip's file name and length in seconds (None without a clip or for one it cannot read)."""
    found, warnings = catalog(str(assets))
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
