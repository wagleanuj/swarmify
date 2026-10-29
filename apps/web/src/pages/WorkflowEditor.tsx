import { Link, useParams, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import YAML from "yaml";

import { api } from "../api/client";
import type { components } from "../api/schema";

type Violation = components["schemas"]["Violation"];
type WorkflowModel = components["schemas"]["WorkflowModel"];
type WorkflowVersion = components["schemas"]["WorkflowVersion"];
type World = components["schemas"]["World"];
type View = "chat" | "stages" | "yaml";
type Doc = Record<string, unknown>;
type RawStage = Record<string, unknown>;

export const STARTER = `name: Weekly short
budget_usd: 5
agents: 4
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week. Check the graph for what we already published.
    done_when: 5 ideas ranked, each with a hook line and a why
    tools: [web_search, graph]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea. Optional note for the script.
    kind: pick
`;

const CHECK_DELAY_MS = 300;
const STAGE_TYPES = ["swarm", "gate", "connector", "workflow"] as const;
type StageType = (typeof STAGE_TYPES)[number];
/** What a stage of each type starts as when added from the list; the check names what is missing. */
const NEW_STAGE: Record<StageType, RawStage> = {
  swarm: { swarm: "new swarm", goal: "", done_when: "" },
  gate: { gate: "review", ask: "Approve?", kind: "approve" },
  connector: { connector: "render", calls: "remotion_render" },
  workflow: { workflow: "child", runs: "" },
};
/** The per-stage fields the list edits. `list` fields are comma separated in the input. */
const FIELDS: Record<StageType, { key: string; label: string; kind: "text" | "long" | "list" | "gate_kind" }[]> = {
  swarm: [
    { key: "goal", label: "Goal", kind: "long" },
    { key: "done_when", label: "Done when", kind: "text" },
    { key: "tools", label: "Tools", kind: "list" },
    { key: "output_type", label: "Output type", kind: "text" },
    { key: "produces", label: "Produces", kind: "list" },
    { key: "graphs", label: "Graphs", kind: "list" },
  ],
  gate: [
    { key: "ask", label: "Ask", kind: "text" },
    { key: "kind", label: "Kind", kind: "gate_kind" },
  ],
  connector: [
    { key: "calls", label: "Calls", kind: "text" },
    { key: "input", label: "Input", kind: "text" },
    { key: "inputs", label: "Inputs", kind: "list" },
    { key: "output", label: "Output", kind: "text" },
  ],
  workflow: [
    { key: "runs", label: "Runs", kind: "text" },
    { key: "take", label: "Take", kind: "list" },
  ],
};

/** The YAML as a plain mapping with a stage list, or null while it does not parse that far. */
export function parseDoc(text: string): { doc: Doc; stages: RawStage[] } | null {
  let doc: unknown;
  try {
    doc = YAML.parse(text);
  } catch {
    return null;
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return null;
  const raw = (doc as Doc).stages;
  const stages = Array.isArray(raw) ? raw.filter((s): s is RawStage => !!s && typeof s === "object") : [];
  return { doc: doc as Doc, stages };
}

export function stageType(stage: RawStage): StageType | null {
  return STAGE_TYPES.find((t) => t in stage) ?? null;
}

/** The mapping back to YAML: block style, lists of names inline. */
export function toYaml(doc: Doc): string {
  const document = new YAML.Document(doc);
  YAML.visit(document, {
    Seq(_, node) {
      if (node.items.every((i) => YAML.isScalar(i))) node.flow = true;
    },
  });
  return document.toString({ lineWidth: 0 });
}

export function WorkflowEditor() {
  const { worldId, workflowId } = useParams({ strict: false }) as {
    worldId: string;
    workflowId?: string;
  };
  const router = useRouter();
  const [worlds, setWorlds] = useState<World[]>([]);
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [text, setText] = useState<string | null>(workflowId ? null : STARTER);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [violations, setViolations] = useState<Violation[] | null>(null);
  const [model, setModel] = useState<WorkflowModel | null>(null);
  const [nextRunAt, setNextRunAt] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [view, setView] = useState<View>("yaml");
  const latestCheck = useRef(0);

  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  useEffect(() => {
    if (!workflowId) return;
    api
      .GET("/api/workflows/{workflow_id}/versions", {
        params: { path: { workflow_id: workflowId } },
      })
      .then(({ data, response }) => {
        if (response.status === 404) throw new Error("gone");
        if (!data) throw new Error("load failed");
        setVersions(data);
        const latest = data[data.length - 1];
        if (latest) {
          setText(latest.yaml);
          setSavedText(latest.yaml);
        }
      })
      .catch(() => setError("Could not load this workflow. It may have been deleted."));
  }, [workflowId]);

  // Inline validation: every rule the save would apply, run as you type, debounced.
  useEffect(() => {
    if (text === null) return;
    const id = ++latestCheck.current;
    setChecking(true);
    const timer = setTimeout(async () => {
      let data;
      try {
        ({ data } = await api.POST("/api/worlds/{world_id}/workflows/check", {
          params: { path: { world_id: worldId } },
          body: { yaml: text, workflow_id: workflowId ?? null },
        }));
      } catch {
        data = undefined;
      }
      if (id !== latestCheck.current) return;
      setChecking(false);
      if (!data) {
        setError("Could not check the workflow. Is the API running?");
        return;
      }
      setError(null);
      setViolations(data.violations);
      setModel(data.model ?? null);
      setNextRunAt(data.next_run_at ?? null);
    }, CHECK_DELAY_MS);
    return () => clearTimeout(timer);
  }, [text, worldId, workflowId]);

  const nextVersion = (versions[versions.length - 1]?.number ?? 0) + 1;
  const dirty = text !== savedText;
  const clean = violations !== null && violations.length === 0;
  const canSave = text !== null && dirty && clean && !checking && !busy;
  const parsed = useMemo(() => (text === null ? null : parseDoc(text)), [text]);

  function edit(next: string) {
    setNotice(null);
    setText(next);
  }

  /** Every stage-list edit rewrites the one text the three views project. */
  function rewrite(mutate: (doc: Doc, stages: RawStage[]) => void) {
    if (!parsed) return;
    const doc = structuredClone(parsed.doc);
    const stages = Array.isArray(doc.stages) ? (doc.stages as RawStage[]) : [];
    doc.stages = stages;
    mutate(doc, stages);
    edit(toYaml(doc));
  }

  function setTop(key: string, value: unknown) {
    rewrite((doc) => {
      if (value === "" || value === null || value === undefined || Number.isNaN(value)) delete doc[key];
      else doc[key] = value;
    });
  }

  function setField(index: number, key: string, value: unknown) {
    rewrite((_, stages) => {
      const stage = stages[index];
      if (!stage) return;
      if (value === "" || (Array.isArray(value) && value.length === 0)) delete stage[key];
      else stage[key] = value;
    });
  }

  function move(index: number, by: -1 | 1) {
    rewrite((_, stages) => {
      const [stage] = stages.splice(index, 1);
      if (stage) stages.splice(index + by, 0, stage);
    });
  }

  function remove(index: number) {
    rewrite((_, stages) => void stages.splice(index, 1));
  }

  function add(type: StageType) {
    rewrite((_, stages) => void stages.push({ ...NEW_STAGE[type] }));
  }

  async function save() {
    if (text === null) return;
    setBusy(true);
    setNotice(null);
    const result = workflowId
      ? await api.POST("/api/workflows/{workflow_id}/versions", {
          params: { path: { workflow_id: workflowId } },
          body: { yaml: text },
        })
      : await api.POST("/api/worlds/{world_id}/workflows", {
          params: { path: { world_id: worldId } },
          body: { yaml: text },
        });
    setBusy(false);
    if (result.error || !result.data) {
      const refused = result.error as { violations?: Violation[] } | undefined;
      if (refused?.violations) setViolations(refused.violations);
      else setError("Could not save the workflow. Try again.");
      return;
    }
    saved(result.data, text);
  }

  /** A version is saved: by the save button, or by Richard when asked to. */
  function saved(version: WorkflowVersion, yaml: string) {
    setSavedText(yaml);
    setNotice(`Saved as version ${version.number}.`);
    if (!workflowId) {
      void router.navigate({
        to: "/worlds/$worldId/workflows/$workflowId",
        params: { worldId, workflowId: version.workflow_id },
      });
      return;
    }
    setVersions((prev) => (prev.some((v) => v.id === version.id) ? prev : [...prev, version]));
  }

  async function copyTo(targetWorld: string) {
    if (!workflowId) return;
    setError(null);
    const { data, error } = await api.POST("/api/workflows/{workflow_id}/copy", {
      params: { path: { workflow_id: workflowId } },
      body: { world_id: targetWorld },
    });
    if (error || !data) {
      const refused = error as { violations?: Violation[] } | undefined;
      setError(
        refused?.violations
          ? `That world cannot run this workflow yet: ${refused.violations.map((v) => v.message).join("; ")}.`
          : "Could not copy the workflow. Try again.",
      );
      return;
    }
    void router.navigate({
      to: "/worlds/$worldId/workflows/$workflowId",
      params: { worldId: targetWorld, workflowId: data.workflow_id },
    });
  }

  const title = model?.name?.trim() || (workflowId ? "Workflow" : "New workflow");
  const schedule = typeof parsed?.doc.schedule === "string" ? parsed.doc.schedule : "";

  return (
    <main className="editor-page">
      <header className="page">
        <div>
          <h1>{title}</h1>
          <p className="sub">
            Describe it to Richard, build the stage list by hand, or write the YAML: three views
            of one workflow, always in step. Every save is a new version and runs keep the version
            they started with. Every run ends with a retro that may propose one skill change for
            you to approve.
          </p>
        </div>
        {workflowId && (
          <Link
            to="/worlds/$worldId/workflows/$workflowId/scorecard"
            params={{ worldId, workflowId }}
            className="button"
          >
            Scorecard
          </Link>
        )}
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="editor">
        <section aria-label="Editor">
          <div className="views" role="tablist" aria-label="Views">
            {(
              [
                ["chat", "Richard"],
                ["stages", "Stages"],
                ["yaml", "YAML"],
              ] as [View, string][]
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={view === id}
                onClick={() => setView(id)}
              >
                {label}
              </button>
            ))}
          </div>
          {/* Kept mounted while another view is open, so the conversation and its session stay. */}
          <div hidden={view !== "chat"}>
            <PlannerChat
              worldId={worldId}
              workflowId={workflowId}
              text={text}
              onDraft={(yaml) => {
                setText(yaml);
                setNotice("Richard updated the draft. Review it in Stages or YAML, then save.");
              }}
              onSaved={saved}
            />
          </div>
          {view === "stages" &&
            (parsed ? (
              <StageList
                doc={parsed.doc}
                stages={parsed.stages}
                nextRunAt={nextRunAt}
                onTop={setTop}
                onField={setField}
                onMove={move}
                onRemove={remove}
                onAdd={add}
              />
            ) : (
              <p className="faint" data-testid="stages-unavailable">
                The stage list opens once the YAML parses. Fix it in the YAML view first.
              </p>
            ))}
          {view === "yaml" && (
            <>
              <label htmlFor="yaml">Workflow YAML</label>
              <textarea
                id="yaml"
                name="yaml"
                value={text ?? ""}
                disabled={text === null}
                spellCheck={false}
                rows={22}
                onChange={(e) => edit(e.target.value)}
              />
            </>
          )}
          <div className="editor-actions">
            <button type="button" className="primary" disabled={!canSave} onClick={save}>
              {busy ? "Saving..." : `Save as version ${nextVersion}`}
            </button>
            <span className="faint" role="status" data-testid="check-status">
              {checking
                ? "Checking..."
                : notice ?? (dirty ? "Unsaved changes." : "Everything here is saved.")}
            </span>
          </div>
          <Problems violations={violations} checking={checking} />
        </section>
        <aside>
          <section aria-label="Stages">
            <h2>Stages</h2>
            {model && model.stages.length > 0 ? (
              <ol className="stage-rail" data-testid="stage-rail">
                {model.stages.map((s, i) => (
                  <li key={i}>
                    <b>{s.name}</b> <span className="faint">{s.type}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="faint">The stage list appears as soon as the YAML parses.</p>
            )}
          </section>
          <section aria-label="Schedule">
            <h2>Schedule</h2>
            <p className="faint" data-testid="schedule">
              {schedule ? (
                <>
                  <code>{schedule}</code> (UTC).{" "}
                  {nextRunAt
                    ? `Next run ${new Date(nextRunAt).toLocaleString()}.`
                    : "Save a valid schedule to see the next run."}
                  {workflowId && !dirty && nextRunAt && " It starts on its own and shows on the runs page."}
                </>
              ) : (
                "None. Add a cron schedule in Stages or YAML to run it on its own."
              )}
            </p>
          </section>
          <section aria-label="Versions">
            <h2>Versions</h2>
            {workflowId ? (
              <ul className="versions" data-testid="versions">
                {[...versions].reverse().map((v) => (
                  <li key={v.id}>
                    <span>
                      <b>v{v.number}</b>{" "}
                      <time dateTime={v.created_at} className="faint">
                        {new Date(v.created_at).toLocaleString()}
                      </time>
                    </span>
                    {v.yaml === text ? (
                      <span className="faint">open</span>
                    ) : (
                      <button type="button" onClick={() => edit(v.yaml)}>
                        Open v{v.number}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="faint">Version 1 is created when you save.</p>
            )}
          </section>
          {workflowId && <CopyTo worlds={worlds.filter((w) => w.id !== worldId)} onCopy={copyTo} />}
        </aside>
      </div>
    </main>
  );
}

function Problems({ violations, checking }: { violations: Violation[] | null; checking: boolean }) {
  if (violations === null) return null;
  if (violations.length === 0) {
    return (
      <p className="ok" data-testid="problems">
        No problems. Every rule passes{checking ? ", rechecking" : ""}.
      </p>
    );
  }
  return (
    <section aria-label="Problems" data-testid="problems">
      <h2>
        {violations.length} {violations.length === 1 ? "problem" : "problems"} to fix before saving
      </h2>
      <ul className="problems">
        {violations.map((v, i) => (
          <li key={i}>
            {v.stage && <b>{v.stage}: </b>}
            {v.message} <code>{v.rule}</code>
          </li>
        ))}
      </ul>
    </section>
  );
}

const asText = (v: unknown) => (v === undefined || v === null ? "" : String(v));
const asList = (v: unknown) => (Array.isArray(v) ? v.map(String).join(", ") : asText(v));
const toList = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

function StageList({
  doc,
  stages,
  nextRunAt,
  onTop,
  onField,
  onMove,
  onRemove,
  onAdd,
}: {
  doc: Doc;
  stages: RawStage[];
  nextRunAt: string | null;
  onTop: (key: string, value: unknown) => void;
  onField: (index: number, key: string, value: unknown) => void;
  onMove: (index: number, by: -1 | 1) => void;
  onRemove: (index: number) => void;
  onAdd: (type: StageType) => void;
}) {
  const [adding, setAdding] = useState<StageType>("swarm");
  return (
    <div data-testid="stage-list">
      <div className="fields">
        <label>
          <span>Name</span>
          <input value={asText(doc.name)} onChange={(e) => onTop("name", e.target.value)} />
        </label>
        <div>
          <label>
            <span>Schedule (cron, UTC)</span>
            <input
              value={asText(doc.schedule)}
              placeholder="0 8 * * 1"
              onChange={(e) => onTop("schedule", e.target.value)}
            />
          </label>
          <span className="faint" data-testid="next-run">
            {nextRunAt
              ? `Next run ${new Date(nextRunAt).toLocaleString()}`
              : asText(doc.schedule)
                ? "Not a cron expression yet."
                : "Runs only when you start it."}
          </span>
        </div>
        <label>
          <span>Budget (USD)</span>
          <input
            type="number"
            min={0}
            step="0.5"
            value={asText(doc.budget_usd)}
            onChange={(e) => onTop("budget_usd", e.target.value === "" ? "" : Number(e.target.value))}
          />
        </label>
        <label>
          <span>Agents</span>
          <input
            type="number"
            min={1}
            step={1}
            value={asText(doc.agents)}
            onChange={(e) => onTop("agents", e.target.value === "" ? "" : Number(e.target.value))}
          />
        </label>
      </div>
      <p className="faint">Edits here rewrite the YAML, so comments typed there are dropped.</p>
      <ol className="stage-cards" data-testid="stage-cards">
        {stages.map((stage, i) => {
          const type = stageType(stage);
          const name = type ? asText(stage[type]) : `stage ${i + 1}`;
          return (
            <li key={i} className="stage-card">
              <div className="row">
                <label>
                  <b>{type ?? "unknown type"}</b>
                  {type && (
                    <input
                      aria-label={`Name of stage ${i + 1}`}
                      value={name}
                      onChange={(e) => onField(i, type, e.target.value)}
                    />
                  )}
                </label>
                <span className="row">
                  <button type="button" aria-label={`Move ${name} up`} disabled={i === 0} onClick={() => onMove(i, -1)}>
                    Up
                  </button>
                  <button
                    type="button"
                    aria-label={`Move ${name} down`}
                    disabled={i === stages.length - 1}
                    onClick={() => onMove(i, 1)}
                  >
                    Down
                  </button>
                  <button type="button" aria-label={`Remove ${name}`} onClick={() => onRemove(i)}>
                    Remove
                  </button>
                </span>
              </div>
              {type ? (
                <div className="fields">
                  {FIELDS[type].map((f) => (
                    <label key={f.key}>
                      <span>{f.label}</span>
                      {f.kind === "gate_kind" ? (
                        <select
                          aria-label={`${f.label} of ${name}`}
                          value={asText(stage[f.key]) || "approve"}
                          onChange={(e) => onField(i, f.key, e.target.value)}
                        >
                          <option value="pick">pick</option>
                          <option value="approve">approve</option>
                          <option value="confirm">confirm</option>
                        </select>
                      ) : f.kind === "long" ? (
                        <textarea
                          aria-label={`${f.label} of ${name}`}
                          rows={2}
                          value={asText(stage[f.key])}
                          onChange={(e) => onField(i, f.key, e.target.value)}
                        />
                      ) : (
                        <input
                          aria-label={`${f.label} of ${name}`}
                          value={f.kind === "list" ? asList(stage[f.key]) : asText(stage[f.key])}
                          placeholder={f.kind === "list" ? "comma separated" : undefined}
                          onChange={(e) =>
                            onField(i, f.key, f.kind === "list" ? toList(e.target.value) : e.target.value)
                          }
                        />
                      )}
                    </label>
                  ))}
                  {type === "connector" && "params" in stage && (
                    <span className="faint">params: edit them in the YAML view.</span>
                  )}
                </div>
              ) : (
                <p className="faint">A stage is one of swarm, gate, connector, workflow. Fix it in YAML.</p>
              )}
            </li>
          );
        })}
      </ol>
      <div className="add-stage">
        <select aria-label="Stage type to add" value={adding} onChange={(e) => setAdding(e.target.value as StageType)}>
          {STAGE_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <button type="button" onClick={() => onAdd(adding)}>
          Add stage
        </button>
      </div>
    </div>
  );
}

type ChatLine = { who: "you" | "richard"; text: string };

/** Richard is the planner (ADR 0004): one message in, a reply and often a whole new draft out. */
function PlannerChat({
  worldId,
  workflowId,
  text,
  onDraft,
  onSaved,
}: {
  worldId: string;
  workflowId?: string;
  text: string | null;
  onDraft: (yaml: string) => void;
  onSaved: (version: WorkflowVersion, yaml: string) => void;
}) {
  const [lines, setLines] = useState<ChatLine[]>([]);
  const [message, setMessage] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [thinking, setThinking] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);

  async function send(e: FormEvent) {
    e.preventDefault();
    const said = message.trim();
    if (!said || thinking) return;
    setMessage("");
    setFailed(null);
    setLines((prev) => [...prev, { who: "you", text: said }]);
    setThinking(true);
    const { data, error, response } = await api.POST("/api/worlds/{world_id}/planner", {
      params: { path: { world_id: worldId } },
      body: { message: said, yaml: text, workflow_id: workflowId ?? null, session_id: sessionId },
    });
    setThinking(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setFailed(typeof detail === "string" ? `${detail} Send it again.` : `Richard did not answer (${response.status}). Send it again.`);
      setMessage(said);
      return;
    }
    setSessionId(data.session_id);
    setLines((prev) => [...prev, { who: "richard", text: data.reply }]);
    if (data.saved) onSaved(data.saved, data.saved.yaml);
    else if (data.yaml) onDraft(data.yaml);
  }

  return (
    <div className="chat" data-testid="planner-chat">
      {lines.length === 0 && (
        <p className="faint">
          Tell Richard what the workflow should do, in plain words. He drafts the stage list,
          asks only about what changes its shape, and puts the result in the editor for you to
          save. Ask him to save when you are happy with it.
        </p>
      )}
      <ul className="chat-log" data-testid="chat-log">
        {lines.map((l, i) => (
          <li key={i} className={l.who}>
            <b>{l.who === "you" ? "You" : "Richard"}</b>
            {l.text}
          </li>
        ))}
        {thinking && (
          <li className="richard" role="status">
            <b>Richard</b>Thinking...
          </li>
        )}
      </ul>
      {failed && <p role="alert">{failed}</p>}
      <form onSubmit={send}>
        <textarea
          aria-label="Message to Richard"
          rows={2}
          value={message}
          placeholder="A weekly short: research five ideas, I pick one, write and render it."
          onChange={(e) => setMessage(e.target.value)}
        />
        <button type="submit" className="primary" disabled={thinking || !message.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}

function CopyTo({ worlds, onCopy }: { worlds: World[]; onCopy: (worldId: string) => void }) {
  const [target, setTarget] = useState("");
  const chosen = target || worlds[0]?.id || "";
  return (
    <section aria-label="Copy" className="copy">
      <h2>Copy to another world</h2>
      {worlds.length === 0 ? (
        <p className="faint">Create a second world to copy this workflow into it.</p>
      ) : (
        <>
          <select aria-label="World to copy to" value={chosen} onChange={(e) => setTarget(e.target.value)}>
            {worlds.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
          <button type="button" onClick={() => onCopy(chosen)}>
            Copy the current version
          </button>
          <p className="faint">
            A new workflow at version 1 there, with the same stages. Connectors must exist in
            that world too.
          </p>
        </>
      )}
    </section>
  );
}
