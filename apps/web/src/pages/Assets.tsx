import { type ChangeEvent, type FormEvent, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { assetsRoute } from "../router";
import { Sheet } from "../Sheet";
import { AUDIO, FileLink, fileUrl, IMAGE, VIDEO } from "./Artifacts";
import { Menu } from "./Menu";

type Asset = components["schemas"]["Asset"];
type Folder = components["schemas"]["OwnedFolder"];

const NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$/;

export const size = (bytes: number) =>
  bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`;

/** A file name the API takes: "Calm Bed (loop).mp3" becomes "Calm-Bed-loop-.mp3". */
export const plainName = (name: string) => name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");

/** The API's reason for a refusal, when it gave one. */
const why = (error: unknown) => {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : null;
};

const count = (n: number) => (n === 1 ? "1 file" : `${n} files`);
const stem = (name: string) => name.replace(/\.[^.]+$/, "");

/** Any audio the browser can play, as a 16-bit mono WAV, cut at `maxSeconds`. */
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

/** A world's Assets: the folders its packages own (ADR 0025), each a group of cards you can play
 * first, then the files its builds read. */
export function Assets() {
  const { worldId } = assetsRoute.useParams();
  const [folders, setFolders] = useState<Folder[] | null>(null);
  const [files, setFiles] = useState<Asset[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [adding, setAdding] = useState<Folder | null>(null);
  const [seen, setSeen] = useState(0);
  const [playing, setPlaying] = useState<string | null>(null);
  const player = useRef<HTMLAudioElement>(null);

  useEffect(() => {
    const path = { params: { path: { world_id: worldId } } };
    api
      .GET("/api/worlds/{world_id}/owned_folders", path)
      .then(({ data }) => {
        if (!data) throw new Error("load failed");
        setFolders(data);
      })
      .catch(() => setError("Could not load the assets. Is the API running?"));
    api
      .GET("/api/worlds/{world_id}/assets", path)
      .then(({ data }) => setFiles(data ?? []))
      .catch(() => undefined);
  }, [worldId, seen]);

  const reload = (message: string | null) => {
    setSaid(message);
    setError(null);
    setSeen((n) => n + 1);
  };

  function play(a: Asset) {
    const audio = player.current;
    if (!audio) return;
    if (playing === a.path) {
      audio.pause();
      setPlaying(null);
      return;
    }
    audio.src = fileUrl(worldId, a.path);
    setPlaying(a.path);
    audio.play().catch(() => setPlaying(null));
  }

  const owned = (a: Asset) => folders?.some((f) => a.path.startsWith(f.path));
  const rest = folders && files ? files.filter((a) => !owned(a)) : null;

  return (
    <main className="assets-page">
      <header className="page">
        <div>
          <h1>Assets</h1>
          <p className="sub">Folders your connectors own, and files your builds can read.</p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {said && <p role="status">{said}</p>}
      <audio ref={player} onEnded={() => setPlaying(null)} onPause={() => setPlaying(null)} hidden />

      {folders?.map((f) => (
        <OwnedGroup
          key={f.path}
          worldId={worldId}
          folder={f}
          files={files && files.filter((a) => a.path.startsWith(f.path))}
          playing={playing}
          onPlay={play}
          onAdd={() => setAdding(f)}
          onChanged={reload}
          onError={setError}
        />
      ))}

      <Files worldId={worldId} files={rest} onChanged={reload} onError={setError} />

      <Sheet open={!!adding} onClose={() => setAdding(null)} label={`Add to ${adding?.label ?? ""}`} className="sheet-form">
        {adding && (
          <AddFile
            worldId={worldId}
            folder={adding}
            taken={(files ?? []).filter((a) => a.path.startsWith(adding.path)).map((a) => stem(a.name))}
            onClose={() => setAdding(null)}
            onAdded={(name) => {
              setAdding(null);
              reload(`Added ${name} to ${adding.label}.`);
            }}
          />
        )}
      </Sheet>
    </main>
  );
}

/** One Owned Folder: a card per file, audio playable first, renamed and deleted from its menu,
 * with its Add button below. What the files mean is the package's business. */
function OwnedGroup({
  worldId,
  folder: f,
  files,
  playing,
  onPlay,
  onAdd,
  onChanged,
  onError,
}: {
  worldId: string;
  folder: Folder;
  files: Asset[] | null;
  playing: string | null;
  onPlay: (a: Asset) => void;
  onAdd: () => void;
  onChanged: (said: string | null) => void;
  onError: (e: string) => void;
}) {
  const [renaming, setRenaming] = useState<string | null>(null);
  const head = `owned-${f.folder}`;

  async function remove(a: Asset) {
    if (!window.confirm(`Delete ${a.name}? ${f.package} will no longer find it. This cannot be undone.`)) return;
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

  return (
    <section className="panel" aria-labelledby={head}>
      <div className="panel-head">
        <h2 id={head}>{f.label}</h2>
        {files && files.length > 0 && <span className="faint">{count(files.length)}</span>}
      </div>
      {files === null ? (
        <p className="faint">Loading {f.label.toLowerCase()}...</p>
      ) : files.length === 0 ? (
        <div className="empty">
          <p>
            Nothing in {f.label} yet. {f.about}
          </p>
        </div>
      ) : (
        <ul className="cards voice-cards">
          {files.map((a) => (
            <li key={a.path} className="card voice-card" aria-label={a.path.slice(f.path.length)}>
              <div className="voice-top">
                {AUDIO.test(a.name) ? (
                  <button type="button" className="play" onClick={() => onPlay(a)} aria-label={`${playing === a.path ? "Pause" : "Play"} ${stem(a.name)}`} aria-pressed={playing === a.path}>
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <path d={playing === a.path ? "M7 5h3v14H7zM14 5h3v14h-3z" : "M8 5l11 7-11 7z"} />
                    </svg>
                  </button>
                ) : (
                  <FileLink a={a} worldId={worldId} className="button play-open">
                    Open
                  </FileLink>
                )}
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
                  <div className="voice-name">
                    <b className="mono">{a.path.slice(f.path.length).replace(/\.[^./]+$/, "")}</b>
                    <span className="faint">
                      <Length worldId={worldId} a={a} />
                      {a.name.split(".").pop()?.toUpperCase()} · {size(a.bytes)}
                    </span>
                  </div>
                )}
                <Menu
                  label={`${a.name} actions`}
                  className="icon"
                  button={<span aria-hidden="true">···</span>}
                  items={[
                    { label: "Rename", note: `keeps it in ${f.label}`, onSelect: () => setRenaming(a.path) },
                    { label: "Delete", note: `${f.package} stops finding it`, onSelect: () => remove(a), danger: true },
                  ]}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="assets-bar">
        <button type="button" className="primary" onClick={onAdd}>
          Add to {f.label}
        </button>
      </div>
    </section>
  );
}

/** An audio file's length, read from its header by the browser: "6.4 s · ". */
function Length({ worldId, a }: { worldId: string; a: Asset }) {
  const [seconds, setSeconds] = useState<number | null>(null);
  if (!AUDIO.test(a.name)) return null;
  return (
    <>
      <audio src={fileUrl(worldId, a.path)} preload="metadata" hidden onLoadedMetadata={(e) => setSeconds(e.currentTarget.duration)} />
      {seconds != null && Number.isFinite(seconds) && `${seconds.toFixed(1)} s · `}
    </>
  );
}

type Take = { blob: Blob; seconds: number | null; url: string; ext: string; how: "uploaded" | "recorded" };

/** The Add sheet of an Owned Folder: a file (for an audio folder, uploaded or recorded here,
 * saved as WAV when the folder takes WAV), its name, and what the operator affirms first. */
function AddFile({ worldId, folder: f, taken, onAdded, onClose }: { worldId: string; folder: Folder; taken: string[]; onAdded: (name: string) => void; onClose: () => void }) {
  const audio = f.accept.some((t) => t.startsWith("audio/"));
  const wavOnly = f.accept.length > 0 && f.accept.every((t) => t === "audio/wav");
  const [mode, setMode] = useState<"upload" | "record">("upload");
  const [take, setTake] = useState<Take | null>(null);
  const [name, setName] = useState("");
  const [affirmed, setAffirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recording, setRecording] = useState<{ stop: () => void; seconds: number } | null>(null);
  const stopRef = useRef<() => void>(undefined);
  // Closing the sheet mid-take lets go of the microphone.
  useEffect(() => () => stopRef.current?.(), []);

  async function use(blob: Blob, how: Take["how"], ext: string) {
    setError(null);
    if (!audio) return setTake({ blob, seconds: null, url: "", ext, how });
    try {
      const { wav, seconds } = await toWav(blob);
      setTake(wavOnly ? { blob: wav, seconds, url: URL.createObjectURL(wav), ext: "wav", how } : { blob, seconds, url: URL.createObjectURL(blob), ext, how });
    } catch {
      setError("That file is not audio this browser can read. Try a WAV, MP3 or M4A.");
    }
  }

  async function pick(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!name) setName(plainName(stem(file.name)).replace(/\./g, "-").slice(0, 60));
    await use(file, "uploaded", file.name.includes(".") ? (file.name.split(".").pop() ?? "").toLowerCase() : "");
  }

  async function record() {
    setError(null);
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setError("The browser did not give access to the microphone. Allow it, or upload a file.");
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
      void use(new Blob(chunks, { type: recorder.mimeType }), "recorded", "wav");
    };
    const stop = () => recorder.state !== "inactive" && recorder.stop();
    stopRef.current = stop;
    const tick = setInterval(() => setRecording({ stop, seconds: (Date.now() - started) / 1000 }), 200);
    recorder.start();
    setTake(null);
    setRecording({ stop, seconds: 0 });
  }

  const nameProblem = !name ? null : !NAME.test(name) ? "Letters, digits, _ or - only, no spaces." : taken.includes(name) ? `${name} is already in ${f.label}.` : null;
  const ready = take && name && !nameProblem && (affirmed || !f.confirm) && !busy;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!ready || !take) return;
    setBusy(true);
    setError(null);
    const file = take.ext ? `${name}.${take.ext}` : name;
    // the body is the file itself, which the typed client cannot send
    const url = `/api/worlds/${encodeURIComponent(worldId)}/assets/${encodeURIComponent(file)}?folder=${encodeURIComponent(f.folder)}`;
    const response = await globalThis.fetch(url, { method: "PUT", body: take.blob }).catch(() => null);
    setBusy(false);
    if (response?.ok) return onAdded(file);
    const detail = await response?.json().then((b: { detail?: unknown }) => b.detail).catch(() => null);
    setError(typeof detail === "string" ? `Not added: ${detail}.` : `Could not add to ${f.label}. Is the API running?`);
  }

  return (
    <form className="add-voice" onSubmit={save}>
      <h2>Add to {f.label}</h2>
      {f.about && <p className="faint">{f.about}</p>}
      {audio && (
        <div className="segmented" role="group" aria-label="Upload or record">
          <button type="button" aria-pressed={mode === "upload"} onClick={() => setMode("upload")}>
            Upload
          </button>
          <button type="button" aria-pressed={mode === "record"} onClick={() => setMode("record")}>
            Record
          </button>
        </div>
      )}
      {mode === "upload" ? (
        <div className="field">
          <label className="button">
            {take?.how === "uploaded" ? "Choose another file" : "Choose a file"}
            <input type="file" accept={audio ? "audio/*" : f.accept.join(",") || undefined} className="visually-hidden" onChange={pick} aria-label="File" />
          </label>
          {wavOnly && <p className="faint">Any audio file; it is saved as WAV.</p>}
        </div>
      ) : (
        <div className="field">
          <p className="faint">Records from your microphone until you stop.</p>
          {recording ? (
            <button type="button" className="danger" onClick={recording.stop}>
              Stop · {recording.seconds.toFixed(0)} s
            </button>
          ) : (
            <button type="button" onClick={record}>
              {take?.how === "recorded" ? "Record again" : "Start recording"}
            </button>
          )}
        </div>
      )}
      {take?.url && (
        <div className="take">
          <audio controls src={take.url} aria-label="Your take" />
          {take.seconds != null && <span className="num">{take.seconds.toFixed(1)} s</span>}
        </div>
      )}
      <div className="field">
        <label>
          Name
          <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="off" aria-invalid={!!nameProblem} aria-describedby="add-name-hint" />
        </label>
        <small id="add-name-hint" className="faint">
          {nameProblem ?? `Plain and unique in ${f.label}.`}
        </small>
      </div>
      {f.confirm && (
        <label className="check">
          <input type="checkbox" checked={affirmed} onChange={(e) => setAffirmed(e.target.checked)} />
          {f.confirm}
        </label>
      )}
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={!ready}>
          {busy ? "Adding..." : "Add"}
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
