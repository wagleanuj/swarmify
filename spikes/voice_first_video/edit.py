"""PROTOTYPE. The edit: every line becomes a clip on one timeline, spaced by its pause; captions
come out in Remotion's Caption shape; a soft music bed is synthesized to the timeline's length."""
import json, sys
import numpy as np, soundfile as sf
pub = sys.argv[1]
takes = json.load(open(f"{pub}/takes.json"))
clips, captions, scenes, at = [], [], [], 0.4  # a short breath before the first word
for take in takes["takes"]:
    s0 = at
    for l in take["lines"]:
        length = l["cut_out"] - l["cut_in"]
        clips.append({"src": take["file"], "from": at, "trimBefore": l["cut_in"], "trimAfter": l["cut_out"]})
        line_no = len(clips) - 1
        for w in l["words"]:
            captions.append({"line": line_no, "text": " " + w["text"], "startMs": round((at + w["start"] - l["cut_in"]) * 1000),
                             "endMs": round((at + w["end"] - l["cut_in"]) * 1000), "timestampMs": None, "confidence": None})
        at += length + l["pause_after"]
    scenes.append({"id": take["scene"], "start": s0, "end": at})
total = at + 0.5
# Music: a gentle C-major arpeggio bed at 120 bpm, quiet; the composition ducks it under speech.
sr, bpm = 44100, 120
t = np.arange(int(total * sr)) / sr
notes = [261.63, 329.63, 392.0, 523.25, 392.0, 329.63, 349.23, 440.0]
step = 60 / bpm / 2
idx = (t // step).astype(int) % len(notes)
f = np.array(notes)[idx]
env = np.exp(-((t % step) / step) * 4)
bed = 0.18 * np.sin(2 * np.pi * f * t) * env + 0.05 * np.sin(2 * np.pi * (f / 2) * t)
fade = np.clip(np.minimum(t / 1.0, (total - t) / 1.5), 0, 1)
sf.write(f"{pub}/music.wav", (bed * fade).astype(np.float32), sr)
json.dump({"title": takes["title"], "duration": total, "clips": clips, "captions": captions, "scenes": scenes},
          open(f"{pub}/timeline.json", "w"), indent=1)
print(f"timeline {total:.1f}s, {len(clips)} clips, {len(captions)} caption words, scenes", [(s["id"], round(s["start"], 1)) for s in scenes])
