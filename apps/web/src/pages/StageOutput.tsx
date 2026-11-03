import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type { Connector } from "../api/connectors";
import { type ArtifactFile, type Call, outcomeOf, type Verdict } from "../api/events";
import type { Run } from "../api/runs";
import { cap, the } from "../skin";

/** Loops a stage may take: the first verdict plus three gap loops. Mirrors swarmify_swarm.MAX_LOOPS. */
const VERDICTS = 4;

/** `runs/<run>/<stage>/notes/trends.md#p3` as "trends.md, paragraph 3". */
function source(ref: string): string {
  const [path = "", p] = ref.split("#p");
  return `${path.split("/").pop()}, paragraph ${p}`;
}

/** The judge's verdicts on a swarm stage and the typed artifacts it projected (ADR 0011). */
export function StageOutput({ verdicts, artifacts }: { verdicts: Verdict[]; artifacts: ArtifactFile[] }) {
  const last = verdicts.at(-1);
  const typed = artifacts
    .filter((a) => a.type !== "verdict" && a.type !== "file")
    .sort((a, b) => Number(a.fields.rank ?? 1e9) - Number(b.fields.rank ?? 1e9));
  const files = artifacts.filter((a) => a.type === "file");
  return (
    <>
      <section className="agents" aria-label="Verdict" data-testid="verdict">
        <h3>{cap(the("judge"))}'s verdict</h3>
        {!last ? (
          <p className="faint">No verdict yet. {cap(the("judge"))} judges once every task on the whiteboard is done.</p>
        ) : (
          <>
            <p>
              <span className={`status status-${last.passed ? "done" : "failed"}`}>{last.passed ? "pass" : "fail"}</span>{" "}
              on loop {last.loop} of {VERDICTS}.
              {!last.passed &&
                (last.loop < VERDICTS
                  ? " The gap tasks below went back on the whiteboard."
                  : " That was the last loop, so the stage failed.")}
            </p>
            <ul>
              {last.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
            {last.gaps.length > 0 && (
              <p data-testid="gaps">Gap tasks: {last.gaps.join("; ")}</p>
            )}
            {verdicts.length > 1 && (
              <ol className="faint" aria-label="Earlier verdicts">
                {verdicts.slice(0, -1).map((v) => (
                  <li key={v.id}>
                    Loop {v.loop}: {v.passed ? "pass" : "fail"}. {v.reasons.join(" ")}
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </section>
      <section className="agents" aria-label="Artifacts" data-testid="artifacts">
        <h3>Artifacts</h3>
        {typed.length === 0 && files.length === 0 && (
          <p className="faint">Nothing yet. On a pass, {the("judge")} turns the stage's notes into its declared output.</p>
        )}
        {typed.length > 0 && (
          <ul className="cards">
            {typed.map((a) => {
              const sources = (a.fields.sources as string[] | undefined) ?? [];
              const outcome = a.type === "publish" ? outcomeOf(a.fields) : null;
              return (
                <li key={a.path} className="card" data-testid="artifact-card">
                  <span className="faint">
                    {a.type}
                    {a.fields.rank != null ? ` · #${String(a.fields.rank)} of ${typed.length}` : ""}
                  </span>
                  <b>{a.title}</b>
                  {typeof a.fields.hook === "string" && <span>Hook: {a.fields.hook}</span>}
                  {typeof a.fields.why === "string" && <span>Why: {a.fields.why}</span>}
                  {outcome && (
                    <span data-testid="outcome">
                      {outcome.line} as of {outcome.day}
                    </span>
                  )}
                  {a.type !== "publish" && (
                    <span className="faint">
                      {sources.length ? `From ${sources.map(source).join("; ")}` : "No source paragraph cited."}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {files.length > 0 && (
          <p className="faint">
            Files:{" "}
            {files
              .map((f) => (f.fields.deleted ? `${f.name} (deleted by retention, record kept)` : f.name))
              .join(", ")}
          </p>
        )}
      </section>
    </>
  );
}

const NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$/;

/** Save as Template (ADR 0028): a build step that finished, of a package that declares Templates,
 * saves its build as one by name. The UI reads the declaration, never the package's name. */
export function SaveAsTemplate({ runId, stage, call, connector }: { runId: string; stage: Run["stages"][number]; call: Call | null | undefined; connector?: Connector }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [replace, setReplace] = useState(false);
  const [taken, setTaken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  if (!connector?.templates || stage.status !== "done" || call?.status !== "done") return null;
  const ready = NAME.test(name) && (!taken || replace) && !busy;

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    const { data, error: refused, response } = await api.POST("/api/runs/{run_id}/stages/{stage_id}/template", {
      params: { path: { run_id: runId, stage_id: stage.id } },
      body: { name, replace },
    });
    setBusy(false);
    if (data) return setSaved(name);
    setTaken(response.status === 409);
    const detail = (refused as { detail?: unknown } | undefined)?.detail;
    setError(typeof detail === "string" ? `Not saved: ${detail}.` : "Could not save the Template. Is the API running?");
  }

  if (saved) return <p role="status">Saved as the Template {saved}. It is on the Assets page.</p>;
  if (!open)
    return (
      <p>
        <button type="button" onClick={() => setOpen(true)}>
          Save as Template
        </button>
      </p>
    );
  return (
    <form className="add-voice" onSubmit={save} aria-label="Save as Template">
      <p className="faint">Later builds can start from this one, without what belonged to this run.</p>
      <div className="field">
        <label>
          Name
          <input
            aria-label="Template name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
              setTaken(false);
              setReplace(false);
            }}
            autoComplete="off"
            aria-invalid={!!name && !NAME.test(name)}
          />
        </label>
        <small className="faint">{name && !NAME.test(name) ? "Letters, digits, _ or - only, no spaces." : "Plain and unique, like cat-shorts."}</small>
      </div>
      {taken && (
        <label className="check">
          <input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
          Replace {name}
        </label>
      )}
      {error && <p role="alert">{error}</p>}
      <div className="actions">
        <button type="submit" className="primary" disabled={!ready}>
          {busy ? "Saving..." : "Save"}
        </button>
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
    </form>
  );
}
