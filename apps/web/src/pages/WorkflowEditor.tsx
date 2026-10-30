import { Link, useNavigate, useParams, useRouter, useSearch } from "@tanstack/react-router";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import YAML from "yaml";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { usePhone } from "../usePhone";
import { Scorecard } from "./Scorecard";

type Violation = components["schemas"]["Violation"];
type WorkflowModel = components["schemas"]["WorkflowModel"];
type WorkflowVersion = components["schemas"]["WorkflowVersion"];
type World = components["schemas"]["World"];
export const EDITOR_TABS = ["stages", "richard", "yaml", "scorecard"] as const;
export type EditorTab = (typeof EDITOR_TABS)[number];
type Doc = Record<string, unknown>;
type RawStage = Record<string, unknown>;

export const STARTER = `name: Weekly short
budget_usd: 5
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
const FIELDS: Record<StageType, { key: string; label: string; kind: "text" | "long" | "list" | "gate_kind" | "count" }[]> = {
  swarm: [
    { key: "goal", label: "Goal", kind: "long" },
    { key: "done_when", label: "Done when", kind: "text" },
    { key: "tools", label: "Tools", kind: "list" },
    { key: "output_type", label: "Output type", kind: "text" },
    { key: "produces", label: "Produces", kind: "list" },
    { key: "graphs", label: "Graphs", kind: "list" },
    { key: "agents", label: "Agents", kind: "count" },
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

/** What a stage needs and makes, from its typed contract: a connector names its inputs and
 * output, a swarm its output type and files, a gate passes on what it was shown. */
export function contract(stages: RawStage[], index: number): { needs: string; makes: string } {
  const stage = stages[index] ?? {};
  const before = index > 0 ? contract(stages, index - 1).makes : "";
  const list = (v: unknown) => (Array.isArray(v) ? v.map(String) : v ? [String(v)] : []);
  switch (stageType(stage)) {
    case "swarm":
      return { needs: before, makes: [...list(stage.output_type), ...list(stage.produces)].join(", ") };
    case "gate":
      return { needs: before, makes: before && stage.kind === "pick" ? `${before} (one picked)` : before };
    case "connector":
      return { needs: [...list(stage.inputs), ...list(stage.input)].join(", "), makes: list(stage.output).join(", ") };
    case "workflow":
      return { needs: "", makes: list(stage.take).join(", ") };
    default:
      return { needs: "", makes: "" };
  }
}

/** The one line a stage row says about what it does. */
function stageWhat(stage: RawStage): string {
  switch (stageType(stage)) {
    case "swarm":
      return asText(stage.goal) || "A swarm with no goal yet";
    case "gate":
      return asText(stage.ask) || "Asks you nothing yet";
    case "connector":
      return `Calls ${asText(stage.calls) || "nothing yet"}`;
    case "workflow":
      return `Runs ${asText(stage.runs) || "nothing yet"}`;
    default:
      return "Not a stage type this editor knows";
  }
}

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
  const [selected, setSelected] = useState(0);
  const [copied, setCopied] = useState<{ world: World; workflowId: string } | null>(null);
  const view = (useSearch({ strict: false }) as { tab?: EditorTab }).tab ?? "stages";
  const navigate = useNavigate();
  const setView = (tab: EditorTab) =>
    void navigate({ to: ".", search: { tab }, replace: true });
  const latestCheck = useRef(0);
  const phone = usePhone();

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
    setSelected(index + by);
  }

  function remove(index: number) {
    rewrite((_, stages) => void stages.splice(index, 1));
    setSelected(Math.max(0, index - 1));
  }

  function add(type: StageType) {
    rewrite((_, stages) => void stages.push({ ...NEW_STAGE[type] }));
    setSelected(parsed?.stages.length ?? 0);
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
    setNotice(`Saved as version ${version.number}; runs already started keep their version.`);
    if (!workflowId) {
      void router.navigate({
        to: "/worlds/$worldId/workflows/$workflowId",
        params: { worldId, workflowId: version.workflow_id },
        search: { tab: view },
      });
      return;
    }
    setVersions((prev) => (prev.some((v) => v.id === version.id) ? prev : [...prev, version]));
  }

  /** A copy is made there and you stay here, with a link to it. */
  async function copyTo(target: World) {
    if (!workflowId) return;
    setError(null);
    setCopied(null);
    const { data, error } = await api.POST("/api/workflows/{workflow_id}/copy", {
      params: { path: { workflow_id: workflowId } },
      body: { world_id: target.id },
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
    setCopied({ world: target, workflowId: data.workflow_id });
  }

  const title = model?.name?.trim() || (workflowId ? "Workflow" : "New workflow");
  const schedule = typeof parsed?.doc.schedule === "string" ? parsed.doc.schedule : "";
  const steps = parsed?.stages ?? [];
  const current = Math.min(selected, Math.max(steps.length - 1, 0));
  const tabs: [EditorTab, string][] = [
    ["stages", "Stages"],
    ["richard", "Richard"],
    ["yaml", "YAML"],
    ...(workflowId ? ([["scorecard", "Scorecard"]] as [EditorTab, string][]) : []),
  ];
  const heading = (
    <div>
      <div className="crumb">
        <Link to="/worlds/$worldId/workflows" params={{ worldId }}>
          Workflows
        </Link>{" "}
        / {title}
      </div>
      <h1>
        {title} {versions.length > 0 && <span className="faint-title">v{versions[versions.length - 1]?.number}</span>}
      </h1>
      <p className="sub">
        {steps.length > 0
          ? `${steps.length} ${steps.length === 1 ? "step" : "steps"}: ${steps
              .map((s, i) => {
                const type = stageType(s);
                return type ? asText(s[type]) : `stage ${i + 1}`;
              })
              .join(", ")}.`
          : "No steps yet."}
      </p>
    </div>
  );

  // A phone reads the workflow; editing it stays on a larger screen.
  if (phone)
    return (
      <main className="editor-page">
        <header className="page">{heading}</header>
        {error && <p role="alert">{error}</p>}
        <p className="faint">Edit on a larger screen.</p>
        {steps.length > 0 && (
          <ol className="stage-rows" data-testid="stage-rows">
            {steps.map((_, i) => (
              <li key={i} className="stage-row">
                <StageSummary stages={steps} index={i} />
              </li>
            ))}
          </ol>
        )}
      </main>
    );

  return (
    <main className="editor-page">
      <header className="page">
        {heading}
        <div className="editor-actions">
          <span className="faint" role="status" data-testid="check-status">
            {checking ? "Checking..." : notice ?? (dirty ? "Unsaved changes." : "Everything here is saved.")}
          </span>
          <button type="button" className="primary" disabled={!canSave} onClick={save}>
            {busy ? "Saving..." : `Save as version ${nextVersion}`}
          </button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="tabs" role="tablist" aria-label="Views">
        {tabs.map(([id, label]) => (
          <button key={id} type="button" role="tab" className="tab" aria-selected={view === id} onClick={() => setView(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="editor">
        <section aria-label="Editor">
          {/* Kept mounted while another view is open, so the conversation and its session stay. */}
          <div hidden={view !== "richard"}>
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
                selected={current}
                nextRunAt={nextRunAt}
                onSelect={setSelected}
                onTop={setTop}
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
              <p className="faint">Comments typed here are dropped when you edit in Stages.</p>
            </>
          )}
          {view === "scorecard" && workflowId && <Scorecard worldId={worldId} workflowId={workflowId} />}
          {view !== "scorecard" && <Problems violations={violations} checking={checking} />}
        </section>
        <aside>
          {view === "stages" && parsed && steps[current] && (
            <StagePanel
              stages={steps}
              index={current}
              agents={asText(parsed.doc.agents) || "1"}
              onField={setField}
              onMove={move}
              onRemove={remove}
            />
          )}
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
          {workflowId && (
            <CopyTo worlds={worlds.filter((w) => w.id !== worldId)} copied={copied} onCopy={copyTo} />
          )}
        </aside>
      </div>
    </main>
  );
}

function Problems({ violations, checking }: { violations: Violation[] | null; checking: boolean }) {
  if (violations === null) return null;
  if (violations.length === 0) {
    return (
      <p className="checks" data-testid="problems">
        <span className="dot live" aria-hidden="true" />
        Checks pass: every step gets the type it needs{checking ? ", rechecking" : ""}.
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
  selected,
  nextRunAt,
  onSelect,
  onTop,
  onAdd,
}: {
  doc: Doc;
  stages: RawStage[];
  selected: number;
  nextRunAt: string | null;
  onSelect: (index: number) => void;
  onTop: (key: string, value: unknown) => void;
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
          <span>Agents per swarm stage</span>
          <input
            type="number"
            min={1}
            step={1}
            placeholder="1"
            value={asText(doc.agents)}
            onChange={(e) => onTop("agents", e.target.value === "" ? "" : Number(e.target.value))}
          />
        </label>
      </div>
      <ol className="stage-rows" data-testid="stage-rows">
        {stages.map((_, i) => (
          <li key={i}>
            <button type="button" className="stage-row" aria-current={i === selected || undefined} onClick={() => onSelect(i)}>
              <StageSummary stages={stages} index={i} />
            </button>
          </li>
        ))}
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

/** A stage row's text: its type and name, what it does, and what it needs and makes. */
function StageSummary({ stages, index }: { stages: RawStage[]; index: number }) {
  const stage = stages[index] ?? {};
  const type = stageType(stage);
  const { needs, makes } = contract(stages, index);
  return (
    <>
      <span className="stage-main">
        <span className="stage-top">
          <span className={`pill type-${type ?? "unknown"}`}>{type ?? "unknown"}</span>
          <b>{stageName(stage, index)}</b>
        </span>
        <span className="muted">{stageWhat(stage)}</span>
      </span>
      <span className="stage-contract faint">
        {needs && <span>needs {needs}</span>}
        <span>
          makes <span className="ink">{makes || "nothing declared"}</span>
        </span>
      </span>
    </>
  );
}

const stageName = (stage: RawStage, index: number) => {
  const type = stageType(stage);
  return type ? asText(stage[type]) : `stage ${index + 1}`;
};

/** The selected stage's settings: its fields, its contract, and where it sits. */
function StagePanel({
  stages,
  index,
  agents,
  onField,
  onMove,
  onRemove,
}: {
  stages: RawStage[];
  index: number;
  /** The workflow's agents per swarm stage, what a stage without its own runs. */
  agents: string;
  onField: (index: number, key: string, value: unknown) => void;
  onMove: (index: number, by: -1 | 1) => void;
  onRemove: (index: number) => void;
}) {
  const stage = stages[index] ?? {};
  const type = stageType(stage);
  const name = stageName(stage, index);
  const { needs, makes } = contract(stages, index);
  return (
    <section aria-label="Stage settings" className="stage-panel" data-testid="stage-panel">
      <div className="row">
        <h2>{name}</h2>
        <span className={`pill type-${type ?? "unknown"}`}>{type ?? "unknown type"}</span>
      </div>
      {type ? (
        <>
          <label>
            <span>Name</span>
            <input aria-label={`Name of stage ${index + 1}`} value={name} onChange={(e) => onField(index, type, e.target.value)} />
          </label>
          {FIELDS[type].map((f) => (
            <label key={f.key}>
              <span>{f.label}</span>
              {f.kind === "gate_kind" ? (
                <select
                  aria-label={`${f.label} of ${name}`}
                  value={asText(stage[f.key]) || "approve"}
                  onChange={(e) => onField(index, f.key, e.target.value)}
                >
                  <option value="pick">pick</option>
                  <option value="approve">approve</option>
                  <option value="confirm">confirm</option>
                </select>
              ) : f.kind === "count" ? (
                <input
                  aria-label={`${f.label} of ${name}`}
                  type="number"
                  min={1}
                  step={1}
                  placeholder={agents}
                  value={asText(stage[f.key])}
                  onChange={(e) => onField(index, f.key, e.target.value === "" ? "" : Number(e.target.value))}
                />
              ) : f.kind === "long" ? (
                <textarea
                  aria-label={`${f.label} of ${name}`}
                  rows={3}
                  value={asText(stage[f.key])}
                  onChange={(e) => onField(index, f.key, e.target.value)}
                />
              ) : (
                <input
                  aria-label={`${f.label} of ${name}`}
                  value={f.kind === "list" ? asList(stage[f.key]) : asText(stage[f.key])}
                  placeholder={f.kind === "list" ? "comma separated" : undefined}
                  onChange={(e) => onField(index, f.key, f.kind === "list" ? toList(e.target.value) : e.target.value)}
                />
              )}
            </label>
          ))}
          {type === "connector" && "params" in stage && <span className="faint">params: edit them in the YAML view.</span>}
        </>
      ) : (
        <p className="faint">A stage is one of swarm, gate, connector, workflow. Fix it in YAML.</p>
      )}
      <dl className="kv" data-testid="stage-contract">
        <dt>Needs</dt>
        <dd>{needs || (index === 0 ? "nothing, it starts the run" : "nothing declared")}</dd>
        <dt>Makes</dt>
        <dd>{makes || "nothing declared"}</dd>
      </dl>
      <span className="row-start">
        <button type="button" aria-label={`Move ${name} up`} disabled={index === 0} onClick={() => onMove(index, -1)}>
          Up
        </button>
        <button
          type="button"
          aria-label={`Move ${name} down`}
          disabled={index === stages.length - 1}
          onClick={() => onMove(index, 1)}
        >
          Down
        </button>
        <button type="button" className="danger" aria-label={`Remove ${name}`} onClick={() => onRemove(index)}>
          Remove
        </button>
      </span>
    </section>
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

function CopyTo({
  worlds,
  copied,
  onCopy,
}: {
  worlds: World[];
  copied: { world: World; workflowId: string } | null;
  onCopy: (world: World) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section aria-label="Copy" className="copy">
      {worlds.length === 0 ? (
        <p className="faint">Create a second world to copy this workflow into it.</p>
      ) : (
        <span className="rel">
          <button type="button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
            Copy to another world
          </button>
          {open && (
            <span className="menu" role="menu">
              {worlds.map((w) => (
                <button
                  key={w.id}
                  type="button"
                  role="menuitem"
                  onClick={() => {
                    setOpen(false);
                    onCopy(w);
                  }}
                >
                  {w.name}
                  <small>Creates a copy there; you stay here</small>
                </button>
              ))}
            </span>
          )}
        </span>
      )}
      {copied && (
        <p className="faint" role="status" data-testid="copied">
          Copied to {copied.world.name} as a new workflow at version 1.{" "}
          <Link
            to="/worlds/$worldId/workflows/$workflowId"
            params={{ worldId: copied.world.id, workflowId: copied.workflowId }}
          >
            Open the copy
          </Link>
        </p>
      )}
    </section>
  );
}
