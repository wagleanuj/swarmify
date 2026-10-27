import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { who } from "../api/tasks";
import { castRoute } from "../router";
import { usd } from "./AgentLog";

type World = components["schemas"]["World"];
type Member = components["schemas"]["CastMember"];
type Version = components["schemas"]["CastEdit"];

const ROLE: Record<string, string> = {
  planner: "seeds every whiteboard",
  worker: "claims tasks, writes notes and artifacts",
  judge: "decides when a stage is done",
};

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** The record as one line: counts with their reference beside them, cost labelled an estimate. */
export function recordLine(m: Member): string {
  const r = m.record ?? { tasks_done: 0, judged: 0, first_try: 0, send_backs: 0, cost_usd: 0 };
  if (m.role !== "worker") {
    return r.cost_usd === 0 ? "No sessions yet." : `Estimated cost so far: ${usd(r.cost_usd)}.`;
  }
  if (r.tasks_done === 0 && r.cost_usd === 0) return "No record yet. It starts with the first run.";
  const parts = [plural(r.tasks_done, "task") + " done"];
  if (r.judged > 0) parts.push(`${r.first_try} of ${plural(r.judged, "stage")} passed first try`);
  parts.push(plural(r.send_backs, "send-back"), `${usd(r.cost_usd)} est.`);
  return parts.join(" · ");
}

export function Cast() {
  const { worldId } = castRoute.useParams();
  const path = { params: { path: { world_id: worldId } } };
  const [world, setWorld] = useState<World | null>(null);
  const [cast, setCast] = useState<Member[] | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [prompt, setPrompt] = useState("");
  const [tools, setTools] = useState("");
  const [history, setHistory] = useState<{ name: string; versions: Version[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data }) => setWorld(data ?? null))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/cast", path)
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setCast(data);
      })
      .catch(() => setError("Could not load the cast. Is the API running?"));
  }, [worldId]); // eslint-disable-line react-hooks/exhaustive-deps

  function startEdit(m: Member) {
    setEditing(m.name);
    setPrompt(m.prompt);
    setTools((m.tools ?? []).join(", "));
    setSaid(null);
    setError(null);
  }

  async function save(e: FormEvent, m: Member) {
    e.preventDefault();
    const next = m.version + 1;
    const ok = window.confirm(
      `Save version ${next} of ${who(m.name)}? Every stage that starts from now on in ${world?.name ?? "this world"} uses it. Runs in flight keep going with what they have.`,
    );
    if (!ok) return;
    setBusy(true);
    setError(null);
    const names = tools.split(",").map((t) => t.trim()).filter(Boolean);
    const { data, error } = await api.PUT("/api/worlds/{world_id}/cast/{name}", {
      params: { path: { world_id: worldId, name: m.name } },
      body: { prompt, tools: names.length ? names : null, confirm: true },
    });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not saved: ${detail}.` : "Not saved. Try again.");
      return;
    }
    setCast((prev) => (prev ?? []).map((c) => (c.name === data.name ? data : c)));
    setEditing(null);
    setSaid(`Saved version ${data.version} of ${who(data.name)}. It applies from the next stage on.`);
    if (history?.name === m.name) void showHistory(m.name);
  }

  async function showHistory(name: string) {
    const { data } = await api.GET("/api/worlds/{world_id}/cast/{name}/versions", {
      params: { path: { world_id: worldId, name } },
    });
    if (data) setHistory({ name, versions: data });
  }

  const name = world?.name ?? "this world";
  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> /{" "}
            <Link to="/worlds/$worldId" params={{ worldId }}>
              {name.toLowerCase()}
            </Link>{" "}
            / cast
          </div>
          <h1>Cast</h1>
          <p className="sub">
            Who works in {name}: Richard plans, the workers claim tasks, Erlich judges. Records
            come from every run so far; cost is the runtime's estimate, not billing.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {said && (
        <p role="status" className="ok">
          {said}
        </p>
      )}
      {cast === null && !error && <p role="status">Loading...</p>}
      <ul className="cast-list" data-testid="cast">
        {(cast ?? []).map((m) => (
          <li key={m.name} className="card cast-member" data-testid={`cast-${m.name}`}>
            <div className="row">
              <h3>
                {who(m.name)} <span className="faint-title">{ROLE[m.role]}</span>
              </h3>
              <span className="chip" title={m.updated_at ? `edited ${new Date(m.updated_at).toLocaleDateString()}` : "the default"}>
                v{m.version}
                {m.version === 1 ? ", default" : ""}
              </span>
            </div>
            <p data-testid="record">{recordLine(m)}</p>
            {editing === m.name ? (
              <form className="cast-edit" onSubmit={(e) => void save(e, m)} aria-label={`Edit ${who(m.name)}`}>
                <label>
                  Standing instructions
                  <textarea
                    rows={3}
                    value={prompt}
                    onChange={(e) => setPrompt(e.target.value)}
                    placeholder="Write hooks first. Cite the note you took it from."
                  />
                </label>
                {m.role === "worker" && (
                  <label>
                    Tools (comma separated; blank for any the stage allows)
                    <input value={tools} onChange={(e) => setTools(e.target.value)} placeholder="web_search, graph" />
                  </label>
                )}
                <p className="faint">
                  Becomes version {m.version + 1}. Applies to every stage that starts from now on, in
                  every run of {name}.
                </p>
                <div className="row">
                  <button type="submit" className="primary" disabled={busy}>
                    Save as version {m.version + 1}
                  </button>
                  <button type="button" onClick={() => setEditing(null)} disabled={busy}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <>
                <p className="faint">
                  {m.prompt ? `Standing instructions: ${m.prompt}` : "No standing instructions; the stage goal is all it gets."}
                </p>
                {m.role === "worker" && (
                  <p className="faint">Tools: {m.tools ? m.tools.join(", ") : "any the stage allows"}</p>
                )}
                <div className="row">
                  <button type="button" onClick={() => startEdit(m)} aria-label={`Edit ${who(m.name)}`}>
                    Edit
                  </button>
                  <button
                    type="button"
                    onClick={() => (history?.name === m.name ? setHistory(null) : void showHistory(m.name))}
                    aria-label={`History of ${who(m.name)}`}
                  >
                    {history?.name === m.name ? "Hide history" : "History"}
                  </button>
                </div>
              </>
            )}
            {history?.name === m.name && (
              <ol className="versions" data-testid="history">
                {history.versions.map((v) => (
                  <li key={v.number}>
                    <span>
                      v{v.number}
                      {v.number === 1 ? " default" : `, ${new Date(v.created_at).toLocaleDateString()}`}
                    </span>
                    <span className="faint">
                      {v.prompt || "no instructions"} · tools: {v.tools ? v.tools.join(", ") : "any"}
                    </span>
                  </li>
                ))}
              </ol>
            )}
          </li>
        ))}
      </ul>
      <p className="faint">
        How many workers there are is a world setting.{" "}
        <Link to="/worlds/$worldId/settings" params={{ worldId }}>
          Change the agent count
        </Link>
        .
      </p>
    </main>
  );
}
