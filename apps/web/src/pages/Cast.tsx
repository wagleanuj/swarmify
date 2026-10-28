import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { who } from "../api/tasks";
import { castRoute } from "../router";
import { usd } from "./AgentLog";
import { ModelSelect, useModels } from "./ModelSelect";

type World = components["schemas"]["World"];
type Member = components["schemas"]["CastMember"];
type Version = components["schemas"]["CastEdit"];
type Skill = components["schemas"]["Skill"];
type SkillVersion = components["schemas"]["SkillVersion"];

const ROLE: Record<string, string> = {
  planner: "seeds every whiteboard",
  worker: "claims tasks, writes notes and artifacts",
  judge: "decides when a stage is done",
};

const MODEL_ROLES = [
  ["planner_model", "Planner model"],
  ["worker_model", "Worker model"],
  ["judge_model", "Judge model"],
  ["retro_model", "Retro model"],
] as const;
type RoleModel = (typeof MODEL_ROLES)[number][0];

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
  const [skills, setSkills] = useState<Skill[] | null | undefined>(undefined);
  const [skillHistory, setSkillHistory] = useState<{ name: string; versions: SkillVersion[] } | null>(null);
  const models = useModels();
  const [roleModels, setRoleModels] = useState<Record<RoleModel, string>>({
    planner_model: "",
    worker_model: "",
    judge_model: "",
    retro_model: "",
  });

  function showWorld(w: World | null) {
    setWorld(w);
    if (w) setRoleModels({
      planner_model: w.planner_model ?? "",
      worker_model: w.worker_model ?? "",
      judge_model: w.judge_model ?? "",
      retro_model: w.retro_model ?? "",
    });
  }

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data }) => showWorld(data ?? null))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/skills", path)
      .then(({ data }) => setSkills(data ?? null))
      .catch(() => setSkills(null));
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

  async function saveModels(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaid(null);
    const body = Object.fromEntries(MODEL_ROLES.map(([key]) => [key, roleModels[key] || null]));
    const { data, error } = await api.PATCH("/api/worlds/{world_id}", { ...path, body });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not saved: ${detail}.` : "Not saved. Try again.");
      return;
    }
    showWorld(data);
    setSaid("Models saved. They apply from the next stage on.");
  }

  async function showSkillHistory(name: string) {
    const { data } = await api.GET("/api/worlds/{world_id}/skills/{name}/versions", {
      params: { path: { world_id: worldId, name } },
    });
    if (data) setSkillHistory({ name, versions: data });
  }

  /** A rollback is a new version with the old content, so the history keeps every step. */
  async function restore(v: SkillVersion, next: number) {
    if (!window.confirm(`Restore v${v.number} of ${v.name} as v${next}? Every stage that starts from now on loads it.`)) return;
    setBusy(true);
    setError(null);
    setSaid(null);
    const { data, error } = await api.POST("/api/worlds/{world_id}/skills", {
      ...path,
      body: { name: v.name, content: v.content, note: `Restored v${v.number}.` },
    });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not restored: ${detail}.` : "Not restored. Try again.");
      return;
    }
    setSkills((prev) => (prev ?? []).map((sk) => (sk.name === data.name ? data : sk)));
    setSaid(`Restored v${v.number} of ${v.name} as v${data.version ?? next}.`);
    void showSkillHistory(v.name);
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
      <form className="card" onSubmit={(e) => void saveModels(e)} aria-label="Models">
        <h2>Models</h2>
        <div className="settings-grid">
          {MODEL_ROLES.map(([key, label]) => (
            <div className="field" key={key}>
              <ModelSelect
                label={label}
                value={roleModels[key]}
                fallback={`World's: ${world?.model ?? models?.default ?? "the default"}`}
                models={models}
                onChange={(m) => setRoleModels((r) => ({ ...r, [key]: m }))}
              />
            </div>
          ))}
        </div>
        <p className="faint">
          A role left on the world's model follows the{" "}
          <Link to="/worlds/$worldId/settings" params={{ worldId }}>
            world setting
          </Link>
          . A workflow stage that names a model runs all its agents on it.
        </p>
        <div className="row">
          <button type="submit" className="primary" disabled={busy}>
            Save models
          </button>
        </div>
      </form>
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
      <section aria-label="Skills" className="skills">
        <h2>Skills</h2>
        <p className="faint">
          Procedures the cast loads when they match a stage. The retro after every run may propose
          one; you approve it on the Needs you page, or make one from the library. A world skill
          wins over a shared one of the same name.
        </p>
        {skills === undefined && <p role="status">Loading skills...</p>}
        {skills === null && <p className="faint">Could not load the skills.</p>}
        {skills?.length === 0 && (
          <p className="faint" data-testid="no-skills">
            No skills yet. The first arrives when you approve a retro proposal or promote a
            library artifact.
          </p>
        )}
        {skills && skills.length > 0 && (
          <ul className="skill-list" data-testid="skills">
            {skills.map((sk) => (
              <li key={sk.name} data-testid={`skill-${sk.name}`}>
                <span className="row">
                  <b>{sk.name}</b>
                  <span className="chip">{sk.source === "shared" ? "shared" : "this world"}</span>
                </span>
                <span>{sk.description || "No description."}</span>
                <span className="faint">
                  {sk.source === "shared" && "From the shared skills folder; every world reads it."}
                  {sk.source === "world" && sk.version === null && "Hand-written in the world folder."}
                  {sk.source === "world" && sk.version !== null && (
                    <>
                      v{sk.version}
                      {sk.run_id ? (
                        <>
                          {" "}
                          · from run{" "}
                          <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: sk.run_id }}>
                            {sk.run_id.slice(-6)}
                          </Link>
                        </>
                      ) : (
                        " · written by you"
                      )}
                    </>
                  )}
                </span>
                {sk.source === "world" && sk.version !== null && (
                  <span className="row">
                    <button
                      type="button"
                      onClick={() => (skillHistory?.name === sk.name ? setSkillHistory(null) : void showSkillHistory(sk.name))}
                      aria-label={`History of skill ${sk.name}`}
                    >
                      {skillHistory?.name === sk.name ? "Hide history" : "History"}
                    </button>
                  </span>
                )}
                {skillHistory?.name === sk.name && sk.version !== null && (
                  <ol className="versions" data-testid="skill-history">
                    {skillHistory.versions.map((v) => (
                      <li key={v.number}>
                        <span>
                          v{v.number}, {new Date(v.created_at).toLocaleDateString()}
                        </span>
                        <span className="faint">
                          {v.retired ? "retired" : v.run_id ? `from run ${v.run_id.slice(-6)}` : "written by you"}
                          {v.note && ` · ${v.note}`}
                        </span>
                        {v.number === sk.version ? (
                          <span className="chip">current</span>
                        ) : (
                          !v.retired && (
                            <button type="button" disabled={busy} onClick={() => void restore(v, (skillHistory.versions.at(-1)?.number ?? 0) + 1)}>
                              Restore v{v.number}
                            </button>
                          )
                        )}
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
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
