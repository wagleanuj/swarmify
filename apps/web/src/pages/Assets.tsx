import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { assetsRoute } from "../router";
import { Sheet } from "../Sheet";
import { AUDIO, FileLink, fileUrl, IMAGE, VIDEO } from "./Artifacts";
import { Menu } from "./Menu";

type Voice = components["schemas"]["Voice"];
type Asset = components["schemas"]["Asset"];

/** What Chatterbox clones from, as the speech connector checks it (README, Voices). */
export const CLIP = { min: 5, max: 10 };
const VOICES = "assets/voices/";
const NAME = /^[A-Za-z0-9_-]{1,60}$/;
const CONSENT = "This is my voice, or I have the speaker's permission";
/** About eight seconds read aloud, the sentence the starter voices speak. */
const READ =
  "On a quiet morning the town wakes slowly, the bakery opens its doors, and people stop to talk about the weather before they walk on to work.";

export const size = (bytes: number) =>
  bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`;

/** A file name the API takes: "Calm Bed (loop).mp3" becomes "Calm-Bed-loop-.mp3". */
export const plainName = (name: string) => name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");

/** The API's reason for a refusal, when it gave one. */
const why = (error: unknown) => {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : null;
};

const and = (names: string[]) =>
  names.length < 2 ? names.join("") : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/** The confirm a delete asks: what goes, and the workflows that lose the voice. */
export function deleteVoiceAsk(v: Pick<Voice, "name" | "used_by">) {
  const lose = [...new Set(v.used_by.map((w) => w.name))];
  if (!lose.length) return `Delete ${v.name}? Its clip and catalog entry go. No workflow uses it.`;
  const one = lose.length === 1;
  return `Delete ${v.name}? ${and(lose)} ${one ? "uses" : "use"} it, and ${one ? "its" : "their"} speech step will no longer find this voice. Its clip and catalog entry go.`;
}

/** Any audio the browser can play, as the 16-bit mono WAV the speech engines take, cut at
 * `maxSeconds`. */
export async function toWav(blob: Blob, maxSeconds = Infinity): Promise<{ wav: Blob; seconds: number }> {
  const audio = await new OfflineAudioContext(1, 1, 24000).decodeAudioData(await blob.arrayBuffer());
  const n = Math.min(audio.length, Math.floor(maxSeconds * audio.sampleRate));
  const channels = Array.from({ length: audio.numberOfChannels }, (_, c) => audio.getChannelData(c));
  const out = new DataView(new ArrayBuffer(44 + n * 2));
  const text = (at: number, s: string) => [...s].forEach((ch, i) => out.setUint8(at + i, ch.charCodeAt(0)));
  text(0, "RIFF");
  out.setUint32(4, 36 + n * 2, true);
  text(8, "WAVEfmt ");
  out.setUint32(16, 16, true);
  out.setUint16(20, 1, true); // PCM
  out.setUint16(22, 1, true); // mono
  out.setUint32(24, audio.sampleRate, true);
  out.setUint32(28, audio.sampleRate * 2, true);
  out.setUint16(32, 2, true);
  out.setUint16(34, 16, true);
  text(36, "data");
  out.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    const mixed = channels.reduce((sum, ch) => sum + (ch[i] ?? 0), 0) / channels.length;
    out.setInt16(44 + i * 2, Math.max(-1, Math.min(1, mixed)) * 0x7fff, true);
  }
  return { wav: new Blob([out.buffer], { type: "audio/wav" }), seconds: n / audio.sampleRate };
}

const clipProblem = (seconds: number) =>
  seconds < CLIP.min || seconds > CLIP.max
    ? `This clip is ${seconds.toFixed(1)} s. Chatterbox clones from ${CLIP.min} to ${CLIP.max} s of speech: trim it, or record one here.`
    : null;

/** A world's Assets: the voices its speech steps can use, then the files its builds read. */
export function Assets() {
  const { worldId } = assetsRoute.useParams();
  const [voices, setVoices] = useState<Voice[] | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [files, setFiles] = useState<Asset[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [seen, setSeen] = useState(0);
  const [playing, setPlaying] = useState<string | null>(null);
  const player = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/voices", { params: { path: { world_id: worldId } } })
      .then(({ data }) => {
        if (!data) throw new Error("load failed");
        // The operator's own voices lead; the seeded starters follow.
        const own = (v: Voice) => (v.consent.startsWith("synthetic") ? 1 : 0);
        setVoices([...data.voices].sort((a, b) => own(a) - own(b)));
        setWarnings(data.warnings);
      })
      .catch(() => setError("Could not load the voices. Is the API running?"));
    api
      .GET("/api/worlds/{world_id}/assets", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setFiles((data ?? []).filter((a) => !a.path.startsWith(VOICES))))
      .catch(() => undefined);
  }, [worldId, seen]);

  const reload = (message: string | null) => {
    setSaid(message);
    setError(null);
    setSeen((n) => n + 1);
  };

  function play(v: Voice) {
    const audio = player.current;
    if (!audio || !v.clip) return;
    if (playing === v.name) {
      audio.pause();
      setPlaying(null);
      return;
    }
    audio.src = fileUrl(worldId, v.clip);
    setPlaying(v.name);
    audio.play().catch(() => setPlaying(null));
  }

  async function remove(v: Voice) {
    if (!window.confirm(deleteVoiceAsk(v))) return;
    const { error } = await api.DELETE("/api/worlds/{world_id}/voices/{name}", { params: { path: { world_id: worldId, name: v.name } } });
    if (error) setError(`Could not delete ${v.name}. Try again.`);
    else reload(`Deleted ${v.name}.`);
  }

  async function edit(v: Voice, description: string, tags: string[]) {
    const { error } = await api.PATCH("/api/worlds/{world_id}/voices/{name}", {
      params: { path: { world_id: worldId, name: v.name } },
      body: { description, tags },
    });
    if (error) {
      setError(why(error) ?? `Could not save ${v.name}.`);
      return false;
    }
    reload(`Saved ${v.name}.`);
    return true;
  }

  return (
    <main className="assets-page">
      <header className="page">
        <div>
          <h1>Assets</h1>
          <p className="sub">Voices your speech steps can use, and files your builds can read.</p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {said && <p role="status">{said}</p>}
      <audio ref={player} onEnded={() => setPlaying(null)} onPause={() => setPlaying(null)} hidden />

      <section className="panel" aria-labelledby="voices-head">
        <div className="panel-head">
          <h2 id="voices-head">Voices</h2>
          {voices && voices.length > 0 && <span className="faint">{voices.length === 1 ? "1 voice" : `${voices.length} voices`}</span>}
        </div>
        {voices === null ? (
          <p className="faint">Loading voices...</p>
        ) : voices.length === 0 ? (
          <div className="empty">
            <p>
              No voices yet. A voice is a 5 to 10 s clip of speech your speech steps clone. Add your own, or give the world eight starter voices with{" "}
              <code>uv run --project connectors/speech python connectors/speech/voices.py {worldId}</code> (the speech package&apos;s README, Voices).
            </p>
          </div>
        ) : (
          <ul className="cards voice-cards">
            {voices.map((v) => (
              <VoiceCard key={v.name} voice={v} playing={playing === v.name} onPlay={() => play(v)} onDelete={() => remove(v)} onEdit={(d, t) => edit(v, d, t)} />
            ))}
          </ul>
        )}
        {warnings.length > 0 && (
          <div className="voice-warnings">
            <p className="faint">voices.json has entries the speech connector ignores:</p>
            <ul>
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </div>
        )}
        <div className="assets-bar">
          <button type="button" className="primary" onClick={() => setAdding(true)}>
            Add a voice
          </button>
        </div>
      </section>

      <Files worldId={worldId} files={files} onChanged={reload} onError={setError} />

      <Sheet open={adding} onClose={() => setAdding(false)} label="Add a voice" className="sheet-form">
        <AddVoice
          worldId={worldId}
          taken={(voices ?? []).map((v) => v.name)}
          onClose={() => setAdding(false)}
          onAdded={(name) => {
            setAdding(false);
            reload(`Added ${name}. Name it in a speech step's voice, or let the script writer pick it.`);
          }}
        />
      </Sheet>
    </main>
  );
}

function VoiceCard({
  voice: v,
  playing,
  onPlay,
  onDelete,
  onEdit,
}: {
  voice: Voice;
  playing: boolean;
  onPlay: () => void;
  onDelete: () => void;
  onEdit: (description: string, tags: string[]) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [description, setDescription] = useState(v.description);
  const [tags, setTags] = useState(v.tags.join(", "));
  const used = [...new Map(v.used_by.map((w) => [w.id, w])).values()];

  async function save(e: FormEvent) {
    e.preventDefault();
    const words = tags.split(",").map((t) => t.trim()).filter(Boolean);
    if (await onEdit(description, words)) setEditing(false);
  }

  return (
    <li className="card voice-card" aria-label={v.name}>
      <div className="voice-top">
        <button type="button" className="play" onClick={onPlay} disabled={!v.clip} aria-label={`${playing ? "Pause" : "Play"} ${v.name}`} aria-pressed={playing}>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d={playing ? "M7 5h3v14H7zM14 5h3v14h-3z" : "M8 5l11 7-11 7z"} />
          </svg>
        </button>
        <div className="voice-name">
          <b className="mono">{v.name}</b>
          <span className="faint">
            {v.seconds != null ? `${v.seconds.toFixed(1)} s clip` : v.preset ? `preset ${v.preset}` : "no clip"} · {v.engine}
          </span>
        </div>
        <Menu
          label={`${v.name} actions`}
          className="icon"
          button={<span aria-hidden="true">···</span>}
          items={[
            { label: "Edit", note: "description and tags", onSelect: () => setEditing(true) },
            { label: "Delete", note: used.length ? `${used.length} workflow${used.length === 1 ? "" : "s"} use it` : "its clip and entry", onSelect: onDelete, danger: true },
          ]}
        />
      </div>
      {editing ? (
        <form className="voice-edit" onSubmit={save}>
          <label>
            Description
            <input value={description} onChange={(e) => setDescription(e.target.value)} required />
          </label>
          <label>
            Tags
            <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="warm, calm, narrator" />
          </label>
          <div className="actions">
            <button type="submit" className="primary">
              Save
            </button>
            <button type="button" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          <p>{v.description}</p>
          {v.tags.length > 0 && (
            <div className="chips">
              {v.tags.map((t) => (
                <span key={t} className="chip">
                  {t}
                </span>
              ))}
            </div>
          )}
        </>
      )}
      <p className="faint used-by">{used.length ? `Used by ${and(used.map((w) => w.name))}` : "Not used by a workflow yet"}</p>
    </li>
  );
}

type Take = { wav: Blob; seconds: number; url: string; how: "uploaded" | "recorded" };

/** The Add a voice sheet: a clip (uploaded or recorded here), a name, what it sounds like, and
 * the consent the catalog requires. */
function AddVoice({ worldId, taken, onAdded, onClose }: { worldId: string; taken: string[]; onAdded: (name: string) => void; onClose: () => void }) {
  const [mode, setMode] = useState<"upload" | "record">("upload");
  const [take, setTake] = useState<Take | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recording, setRecording] = useState<{ stop: () => void; seconds: number } | null>(null);
  const stopRef = useRef<() => void>(undefined);
  // Closing the sheet mid-take lets go of the microphone.
  useEffect(() => () => stopRef.current?.(), []);

  async function use(blob: Blob, how: Take["how"]) {
    setError(null);
    try {
      const { wav, seconds } = await toWav(blob, how === "recorded" ? CLIP.max : Infinity);
      setTake({ wav, seconds, url: URL.createObjectURL(wav), how });
    } catch {
      setError("That file is not audio this browser can read. Try a WAV, MP3 or M4A.");
    }
  }

  async function pick(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!name) setName(plainName(file.name.replace(/\.[^.]+$/, "")).replace(/\./g, "-").slice(0, 60));
    await use(file, "uploaded");
  }

  async function record() {
    setError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError("The browser did not give access to the microphone. Allow it, or upload a clip.");
      return;
    }
    const recorder = new MediaRecorder(stream);
    const chunks: Blob[] = [];
    const started = Date.now();
    recorder.ondataavailable = (e) => chunks.push(e.data);
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      clearInterval(tick);
      setRecording(null);
      void use(new Blob(chunks, { type: recorder.mimeType }), "recorded");
    };
    const stop = () => recorder.state !== "inactive" && recorder.stop();
    stopRef.current = stop;
    const tick = setInterval(() => {
      const seconds = (Date.now() - started) / 1000;
      if (seconds >= CLIP.max) stop();
      else setRecording({ stop, seconds });
    }, 200);
    recorder.start();
    setTake(null);
    setRecording({ stop, seconds: 0 });
  }

  const problem = take ? clipProblem(take.seconds) : null;
  const nameProblem = !name ? null : !NAME.test(name) ? "Letters, digits, _ or - only, no spaces." : taken.includes(name) ? `A voice is already named ${name}.` : null;
  const ready = take && !problem && name && !nameProblem && description.trim() && consent && !busy;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!ready || !take) return;
    setBusy(true);
    setError(null);
    const form = new FormData();
    form.set("name", name);
    form.set("description", description);
    form.set("tags", tags);
    form.set("consent", `${CONSENT} (${take.how} in Swarmify, ${new Date().toISOString().slice(0, 10)})`);
    form.set("clip", take.wav, `${name}.wav`);
    // multipart with a file, which the typed client cannot send
    const response = await globalThis
      .fetch(`/api/worlds/${encodeURIComponent(worldId)}/voices`, { method: "POST", body: form })
      .catch(() => null);
    setBusy(false);
    if (response?.ok) return onAdded(name);
    const detail = await response?.json().then((b: { detail?: unknown }) => b.detail).catch(() => null);
    setError(typeof detail === "string" ? `Not added: ${detail}.` : "Could not add the voice. Is the API running?");
  }

  return (
    <form className="add-voice" onSubmit={save}>
      <h2>Add a voice</h2>
      <div className="segmented" role="group" aria-label="Clip">
        <button type="button" aria-pressed={mode === "upload"} onClick={() => setMode("upload")}>
          Upload clip
        </button>
        <button type="button" aria-pressed={mode === "record"} onClick={() => setMode("record")}>
          Record
        </button>
      </div>
      {mode === "upload" ? (
        <div className="field">
          <label className="button">
            {take?.how === "uploaded" ? "Choose another clip" : "Choose a clip"}
            <input type="file" accept="audio/*" className="visually-hidden" onChange={pick} aria-label="Clip file" />
          </label>
          <p className="faint">
            {CLIP.min} to {CLIP.max} s of clear speech, one speaker, no music. Any audio file; it is saved as WAV.
          </p>
        </div>
      ) : (
        <div className="field">
          <p className="read-this">{READ}</p>
          <p className="faint">Read this in your normal voice. Recording stops at {CLIP.max} s.</p>
          {recording ? (
            <button type="button" className="danger" onClick={recording.stop}>
              Stop · {recording.seconds.toFixed(0)} of {CLIP.max} s
            </button>
          ) : (
            <button type="button" onClick={record}>
              {take?.how === "recorded" ? "Record again" : "Start recording"}
            </button>
          )}
        </div>
      )}
      {take && (
        <div className="take">
          <audio controls src={take.url} aria-label="Your clip" />
          <span className="num">{take.seconds.toFixed(1)} s</span>
        </div>
      )}
      {problem && <p role="alert">{problem}</p>}
      <div className="field">
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="narrator" autoComplete="off" aria-invalid={!!nameProblem} aria-describedby="voice-name-hint" />
        </label>
        <small id="voice-name-hint" className="faint">
          {nameProblem ?? "Plain and unique: what a speech step's voice says."}
        </small>
      </div>
      <label>
        Description
        <input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Warm and calm, for bedtime facts" />
      </label>
      <label>
        Tags
        <input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="warm, calm, narrator" />
      </label>
      <label className="check">
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        {CONSENT}
      </label>
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={!ready}>
          {busy ? "Adding..." : "Add voice"}
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** Everything else in the assets folder: images, music, video, fonts, any file a build reads. */
function Files({ worldId, files, onChanged, onError }: { worldId: string; files: Asset[] | null; onChanged: (said: string | null) => void; onError: (e: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState<string | null>(null);

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const picked = [...(e.target.files ?? [])];
    e.target.value = "";
    setBusy(true);
    const failed: string[] = [];
    for (const file of picked) {
      // the body is the file itself, which the typed client cannot send
      const url = `/api/worlds/${encodeURIComponent(worldId)}/assets/${encodeURIComponent(plainName(file.name))}`;
      const response = await globalThis.fetch(url, { method: "PUT", body: file }).catch(() => null);
      if (!response?.ok) failed.push(file.name);
    }
    setBusy(false);
    onChanged(failed.length ? null : `Uploaded ${picked.length === 1 ? picked[0]?.name : `${picked.length} files`}.`);
    if (failed.length) onError(`Could not upload ${failed.join(", ")}. Try again.`);
  }

  async function remove(a: Asset) {
    if (!window.confirm(`Delete ${a.name}? Builds that read it will not find it. This cannot be undone.`)) return;
    const { error } = await api.DELETE("/api/worlds/{world_id}/assets/{path}", { params: { path: { world_id: worldId, path: a.path } } });
    if (error) onError(`Could not delete ${a.name}. Try again.`);
    else onChanged(`Deleted ${a.name}.`);
  }

  async function rename(a: Asset, name: string) {
    const { error } = await api.PATCH("/api/worlds/{world_id}/assets/{path}", {
      params: { path: { world_id: worldId, path: a.path } },
      body: { name: plainName(name) },
    });
    if (error) return onError(why(error) ? `Not renamed: ${why(error)}.` : `Could not rename ${a.name}.`);
    setRenaming(null);
    onChanged(`Renamed ${a.name} to ${plainName(name)}.`);
  }

  const button = (
    <label className="button">
      {busy ? "Uploading..." : "Upload files"}
      <input type="file" multiple className="visually-hidden" disabled={busy} onChange={upload} aria-label="Upload files" />
    </label>
  );

  return (
    <section className="panel" aria-labelledby="files-head">
      <div className="panel-head">
        <h2 id="files-head">Files</h2>
        {files && files.length > 0 && button}
      </div>
      {files === null ? (
        <p className="faint">Loading files...</p>
      ) : files.length === 0 ? (
        <div className="empty">
          <p>Put images, music or fonts here for your builds to use.</p>
          {button}
        </div>
      ) : (
        <ul className="file-rows">
          {files.map((a) => {
            const url = fileUrl(worldId, a.path);
            const shown = a.path.replace(/^assets\//, "");
            return (
              <li key={a.path} aria-label={shown}>
                <span className="file-preview" aria-hidden="true">
                  {IMAGE.test(a.name) ? (
                    <img src={url} alt="" loading="lazy" />
                  ) : VIDEO.test(a.name) ? (
                    <video src={`${url}#t=0.5`} preload="metadata" muted />
                  ) : (
                    <svg viewBox="0 0 24 24">
                      <path d={AUDIO.test(a.name) ? "M9 17V5l11-2v12M4 17a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0M15 15a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0" : "M6 3h8l4 4v14H6zM14 3v4h4"} />
                    </svg>
                  )}
                </span>
                {renaming === a.path ? (
                  <form
                    className="file-rename"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void rename(a, new FormData(e.currentTarget).get("name") as string);
                    }}
                  >
                    <input name="name" defaultValue={a.name} aria-label={`New name for ${a.name}`} autoFocus />
                    <button type="submit" className="primary">
                      Rename
                    </button>
                    <button type="button" onClick={() => setRenaming(null)}>
                      Cancel
                    </button>
                  </form>
                ) : (
                  <span className="file-name">
                    <span className="mono">{shown}</span>
                    <span className="faint">{size(a.bytes)}</span>
                  </span>
                )}
                {AUDIO.test(a.name) && <audio className="file-audio" controls preload="none" src={url} aria-label={`Play ${a.name}`} />}
                <FileLink a={a} worldId={worldId} className="file-open">
                  Open
                </FileLink>
                <Menu
                  label={`${a.name} actions`}
                  className="icon"
                  button={<span aria-hidden="true">···</span>}
                  items={[
                    { label: "Rename", note: "keeps it in its folder", onSelect: () => setRenaming(a.path) },
                    { label: "Delete", note: "builds stop finding it", onSelect: () => remove(a), danger: true },
                  ]}
                />
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
