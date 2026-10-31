import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { who } from "../api/tasks";
import { castRoute } from "../router";
import { cap, the } from "../skin";
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

/** The world setting each role's model lives in; every worker shares one. */
const MODEL_OF: Record<string, RoleModel> = { planner: "planner_model", worker: "worker_model", judge: "judge_model" };
type RoleModel = "planner_model" | "worker_model" | "judge_model" | "retro_model";

const NO_RECORD = { tasks_done: 0, judged: 0, first_try: 0, send_backs: 0, cost_usd: 0 };

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
  const [saved, setSaved] = useState<{ name: string; text: string } | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [skills, setSkills] = useState<Skill[] | null | undefined>(undefined);
  const [skillHistory, setSkillHistory] = useState<{ name: string; versions: SkillVersion[] } | null>(null);
  const models = useModels();

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data }) => setWorld(data ?? null))
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
    setSaved(null);
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
    setSaved({ name: data.name, text: `Saved version ${data.version} of ${who(data.name)}. It applies from the next stage on.` });
    if (history?.name === m.name) void showHistory(m.name);
  }

  /** A model change saves itself, stamped in the row it was made in. */
  async function saveModel(key: RoleModel, value: string, row: string) {
    setError(null);
    setSaved(null);
    const { data, error } = await api.PATCH("/api/worlds/{world_id}", { ...path, body: { [key]: value || null } });
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not saved: ${detail}.` : "Not saved. Try again.");
      return;
    }
    setWorld(data);
    setSaved({ name: row, text: "Saved" });
  }

  const modelSelect = (key: RoleModel, label: string, row: string) => (
    <span className="model-cell">
      <ModelSelect
        label={label}
        hideLabel
        value={world?.[key] ?? ""}
        fallback={`World's: ${world?.model ?? models?.default ?? "the default"}`}
        models={models}
        onChange={(m) => void saveModel(key, m, row)}
      />
      {saved?.name === row && saved.text === "Saved" && (
        <span role="status" className="faint">
          Saved
        </span>
      )}
    </span>
  );

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
  const total = (cast ?? []).reduce((sum, m) => sum + (m.record ?? NO_RECORD).cost_usd, 0);
  return (
    <main>
      <header className="page">
        <div>
          <h1>Cast</h1>
          <p className="sub">
            {cast
              ? `${cast.length} agents work in ${name}: ${cap(the("planner"))} plans, the workers claim tasks, ${the("judge")} judges. Together they cost an estimated ${usd(total)} so far.`
              : `Who works in ${name}.`}
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
      <section className="panel" aria-labelledby="agents">
        <div className="panel-head">
          <h2 id="agents">Agents</h2>
          <span className="faint">
            First try is a stage the judge passed without a send back. A model change saves itself; the workers share one.
          </span>
        </div>
        <div className="scroll-x cast-scroll">
          <table className="cast-table" data-testid="cast">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Model</th>
                <th className="num">Tasks</th>
                <th className="num">Judged</th>
                <th className="num">First try</th>
                <th className="num">Send backs</th>
                <th className="num">Cost</th>
                <th>
                  <span className="visually-hidden">Actions</span>
                </th>
              </tr>
            </thead>
            {(cast ?? []).map((m) => {
              const r = m.record ?? NO_RECORD;
              const key = MODEL_OF[m.role];
              return (
                <tbody key={m.name} className="cast-member" data-testid={`cast-${m.name}`}>
                  <tr>
                    <td>
                      <b>{who(m.name)}</b>
                      <span className="faint">
                        {ROLE[m.role] ?? m.role} · v{m.version}
                        {m.version === 1 ? ", default" : ""}
                      </span>
                    </td>
                    <td>{key ? modelSelect(key, `Model for ${who(m.name)}`, m.name) : <span className="faint">-</span>}</td>
                    <td className="num">{r.tasks_done}</td>
                    <td className="num">{r.judged}</td>
                    <td className="num">
                      {r.judged ? (
                        <>
                          {r.first_try} <span className="faint">of {r.judged}</span>
                        </>
                      ) : (
                        <span className="faint">-</span>
                      )}
                    </td>
                    <td className="num">{r.send_backs}</td>
                    <td className="num">{usd(r.cost_usd)}</td>
                    <td className="cast-actions">
                      {editing !== m.name && (
                        <button type="button" onClick={() => startEdit(m)} aria-label={`Edit ${who(m.name)}`}>
                          Edit
                        </button>
                      )}
                      <button
                        type="button"
                        className="link"
                        onClick={() => (history?.name === m.name ? setHistory(null) : void showHistory(m.name))}
                        aria-label={`History of ${who(m.name)}`}
                      >
                        {history?.name === m.name ? "Hide history" : "History"}
                      </button>
                    </td>
                  </tr>
                  {saved?.name === m.name && saved.text !== "Saved" && (
                    <tr>
                      <td colSpan={8} className="cast-more">
                        <span role="status" className="ok">
                          {saved.text}
                        </span>
                      </td>
                    </tr>
                  )}
                  {editing === m.name && (
                    <tr>
                      <td colSpan={8} className="cast-more">
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
                          <div className="actions">
                            <button type="submit" className="primary" disabled={busy}>
                              Save as version {m.version + 1}
                            </button>
                            <button type="button" onClick={() => setEditing(null)} disabled={busy}>
                              Cancel
                            </button>
                            <span className="faint">
                              Becomes version {m.version + 1} for every stage that starts from now on in {name}. Runs
                              already going keep version {m.version}.
                            </span>
                          </div>
                        </form>
                      </td>
                    </tr>
                  )}
                  {history?.name === m.name && (
                    <tr>
                      <td colSpan={8} className="cast-more">
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
                      </td>
                    </tr>
                  )}
                </tbody>
              );
            })}
            <tfoot>
              <tr>
                <td>Retro</td>
                <td>{modelSelect("retro_model", "Retro model", "retro")}</td>
                <td colSpan={4} className="faint">
                  {cast?.length ?? 0} agents, and the retro after every run
                </td>
                <td className="num">{usd(total)}</td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
        <p className="faint">
          A role left on the world's model follows the{" "}
          <Link to="/worlds/$worldId/settings" params={{ worldId }}>
            world setting
          </Link>
          . A workflow stage that names a model runs all its agents on it. Cost is the runtime's estimate, not billing.
        </p>
      </section>
      <section aria-labelledby="skills" className="panel skills">
        <div className="panel-head">
          <h2 id="skills">Skills</h2>
          {skills && <span className="faint">{skills.length} in this world</span>}
        </div>
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
