"""PROTOTYPE. One Chatterbox Turbo take per scene, word times by faster-whisper matched to the
script, and a cut point per line placed at the quietest 10 ms near each line boundary."""
import difflib, json, re, sys, time
import numpy as np, soundfile as sf, torch

out = sys.argv[1]
script = json.load(open("script.json"))
T = {}
t = time.time()
from chatterbox.tts_turbo import ChatterboxTurboTTS
tts = ChatterboxTurboTTS.from_pretrained(device="mps" if torch.backends.mps.is_available() else "cpu")
T["load_tts"] = time.time() - t
t = time.time()
from faster_whisper import WhisperModel
asr = WhisperModel("small.en", device="cpu", compute_type="int8")
T["load_whisper"] = time.time() - t
norm = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())
takes, T["tts"], T["align"] = [], 0.0, 0.0
for scene in script["scenes"]:
    text = " ".join(l["text"] for l in scene["lines"])
    t = time.time()
    torch.manual_seed(7)
    wav = tts.generate(text).squeeze(0).cpu().numpy()
    T["tts"] += time.time() - t
    sr = tts.sr
    path = f"{out}/take_{scene['id']}.wav"
    sf.write(path, wav, sr)
    t = time.time()
    segs, _ = asr.transcribe(path, word_timestamps=True, language="en")
    heard = [(w.word.strip(), w.start, w.end) for s in segs for w in s.words]
    # Match heard words to script words; the script's spelling wins, the times come from audio.
    want = [(li, w) for li, l in enumerate(scene["lines"]) for w in l["text"].split()]
    sm = difflib.SequenceMatcher(a=[norm(w) for _, w in want], b=[norm(h[0]) for h in heard], autojunk=False)
    times = [None] * len(want)
    for a, b, n in sm.get_matching_blocks():
        for k in range(n):
            times[a + k] = heard[b + k][1:]
    for i in range(len(times)):  # fill unmatched words between their neighbours
        if times[i] is None:
            prev = next((times[j][1] for j in range(i - 1, -1, -1) if times[j]), 0.0)
            nxt = next((times[j][0] for j in range(i + 1, len(times)) if times[j]), len(wav) / sr)
            times[i] = (prev, nxt)
    # Cut points: quietest 10 ms window between one line's last word and the next line's first.
    frame = int(sr * 0.01)
    rms = np.sqrt(np.convolve(wav ** 2, np.ones(frame) / frame, mode="same"))
    def quiet(a, b):
        ia, ib = int(a * sr), max(int(b * sr), int(a * sr) + 1)
        return (ia + int(np.argmin(rms[ia:ib]))) / sr
    lines = []
    for li, line in enumerate(scene["lines"]):
        idx = [i for i, (l, _) in enumerate(want) if l == li]
        first, last = times[idx[0]][0], times[idx[-1]][1]
        lines.append({"text": line["text"], "pause_after": line["pause_after"], "first": first, "last": last,
                      "words": [{"text": want[i][1], "start": times[i][0], "end": times[i][1]} for i in idx]})
    dur = len(wav) / sr
    for li, l in enumerate(lines):
        l["cut_in"] = 0.0 if li == 0 else quiet(lines[li - 1]["last"], l["first"])
        l["cut_out"] = dur if li == len(lines) - 1 else quiet(l["last"], lines[li + 1]["first"])
        if li == 0: l["cut_in"] = max(0.0, l["first"] - 0.05)
        if li == len(lines) - 1: l["cut_out"] = min(dur, l["last"] + 0.15)
    T["align"] += time.time() - t
    takes.append({"scene": scene["id"], "file": f"take_{scene['id']}.wav", "duration": dur, "lines": lines})
    print(f"{scene['id']}: {dur:.1f}s take, heard {len(heard)} words for {len(want)} script words", flush=True)
json.dump({"title": script["title"], "takes": takes}, open(f"{out}/takes.json", "w"), indent=1)
print("TIMES", json.dumps({k: round(v, 1) for k, v in T.items()}))
