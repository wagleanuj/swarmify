import { Link, useBlocker, useNavigate, useParams, useRouter, useSearch } from "@tanstack/react-router";
import { type FormEvent, useEffect, useMemo, useRef, useState } from "react";
import YAML from "yaml";

import { useConfirm } from "../Dialog";
import { api } from "../api/client";
import { useConnectors } from "../api/connectors";
import type { components } from "../api/schema";
import { Icon } from "../Icon";
import { Sheet } from "../Sheet";
import { notify } from "../Toast";
import { Loading, Skeleton } from "../Skeleton";
import { role } from "../skin";
import { usePhone } from "../usePhone";
import { Scorecard } from "./Scorecard";

type Violation = components["schemas"]["Violation"];
type WorkflowModel = components["schemas"]["WorkflowModel"];
type WorkflowVersion = components["schemas"]["WorkflowVersion"];
type Starter = components["schemas"]["Starter"];
type World = components["schemas"]["World"];
type Template = components["schemas"]["Template"];
export const EDITOR_TABS = ["stages", "planner", "yaml", "scorecard"] as const;
export type EditorTab = (typeof EDITOR_TABS)[number];
type Doc = Record<string, unknown>;
type RawStage = Record<string, unknown>;

const CHECK_DELAY_MS = 300;
/** How long "Removed research. Undo" stays in the save bar. */
const UNDO_MS = 6000;
const LEAVE = { title: "Leave without saving?", lines: ["Your changes to this draft are lost."], action: "Leave", danger: true };
const STAGE_TYPES = ["swarm", "gate", "connector", "workflow"] as const;
const GATE_KINDS = ["pick", "approve", "confirm"] as const;
type StageType = (typeof STAGE_TYPES)[number];
/** What a stage of each type starts as when added from the list; the check names what is missing. */
const NEW_STAGE: Record<StageType, RawStage> = {
  swarm: { swarm: "new swarm", goal: "", done_when: "" },
  gate: { gate: "review", ask: "Approve?", kind: "approve" },
  connector: { connector: "new step", calls: "" },
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
  const [confirm, dialog] = useConfirm();
  const { worldId, workflowId } = useParams({ strict: false }) as {
    worldId: string;
    workflowId?: string;
  };
  const router = useRouter();
  const [worlds, setWorlds] = useState<World[]>([]);
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [starters, setStarters] = useState<Starter[]>([]);
  const [text, setText] = useState<string | null>(null);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [violations, setViolations] = useState<Violation[] | null>(null);
  const [warnings, setWarnings] = useState<Violation[]>([]);
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
  const connectors = useConnectors(worldId);
  const [templates, setTemplates] = useState<Template[]>([]);
  // A phone edits a stage in a sheet over the list; a wider screen in the aside.
  const [sheet, setSheet] = useState(false);
  // The last removal, undone in one step from the text before it.
  const [removed, setRemoved] = useState<string | null>(null);
  const beforeRemove = useRef<string | null>(null);

  useEffect(() => {
    if (!removed) return;
    const t = setTimeout(() => setRemoved(null), UNDO_MS);
    return () => clearTimeout(t);
  }, [removed]);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/templates", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setTemplates(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  // A new workflow opens on the first starter; the others sit above the tabs.
  useEffect(() => {
    if (workflowId) return;
    api
      .GET("/api/workflows/starters")
      .then(({ data }) => {
        if (!data) throw new Error("load failed");
        setStarters(data);
        setText((t) => t ?? data[0]?.yaml ?? "");
      })
      .catch(() => {
        setText((t) => t ?? "");
        setError("Could not load the starter workflows. Write one in YAML, or reload.");
      });
  }, [workflowId]);

  // A new workflow's first save moves here; what was typed while it saved stays the draft.
  const firstSave = useRef<string | null>(null);
  useEffect(() => {
    if (!workflowId) return;
    const keep = firstSave.current === workflowId;
    api
      .GET("/api/workflows/{workflow_id}/versions", {
        params: { path: { workflow_id: workflowId } },
      })
      .then(({ data, response }) => {
        if (response.status === 404) throw new Error("gone");
        if (!data) throw new Error("load failed");
        setVersions(data);
        // a workflow with no version yet opens empty rather than loading forever
        const yaml = data[data.length - 1]?.yaml ?? "";
        firstSave.current = null;
        setText((t) => (keep && t !== null ? t : yaml));
        setSavedText(yaml);
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
      setWarnings(data.warnings ?? []);
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
  // An untouched starter is nothing to lose.
  const untouched = !text?.trim() || starters.some((s) => s.yaml === text);
  // Read when the router asks, so a save that navigates at once is not held by stale state.
  const unsaved = useRef(false);
  unsaved.current = text !== null && dirty && !untouched;
  useBlocker({
    // switching tabs changes only the search, never the page
    shouldBlockFn: async ({ current, next }) =>
      unsaved.current && current.pathname !== next.pathname && !(await confirm(LEAVE)),
    enableBeforeUnload: () => unsaved.current,
  });

  function edit(next: string) {
    setNotice(null);
    setText(next);
    // Undo restores the text before a removal, so any later edit retires it rather than lose itself.
    setRemoved(null);
    beforeRemove.current = null;
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
      // an emptied name keeps its key: without it the stage has no type
      if ((value === "" && key !== stageType(stage)) || (Array.isArray(value) && value.length === 0)) delete stage[key];
      else stage[key] = value;
    });
  }

  /** A connector stage's Template (ADR 0028), its `params[param]`; none starts from the starter. */
  function setBase(index: number, param: string, base: string) {
    rewrite((_, stages) => {
      const stage = stages[index];
      if (!stage) return;
      const params = { ...(stage.params as Doc | undefined) };
      if (base) params[param] = base;
      else delete params[param];
      if (Object.keys(params).length) stage.params = params;
      else delete stage.params;
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
    const stage = parsed?.stages[index];
    const before = text;
    rewrite((_, stages) => void stages.splice(index, 1));
    beforeRemove.current = before;
    setSelected(Math.max(0, index - 1));
    setSheet(false);
    setRemoved(stage ? stageName(stage, index) : `stage ${index + 1}`);
  }

  function undo() {
    if (beforeRemove.current !== null) edit(beforeRemove.current);
    beforeRemove.current = null;
    setRemoved(null);
  }

  function add(type: StageType) {
    rewrite((_, stages) => void stages.push({ ...NEW_STAGE[type] }));
    setSelected(parsed?.stages.length ?? 0);
    setSheet(true);
  }

  function open(index: number) {
    setSelected(index);
    setSheet(true);
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

  /** Swap the draft for a starter; asks first when the draft is more than an untouched starter. */
  async function begin(starter: Starter) {
    const ask = { title: `Start over from ${starter.name}?`, lines: ["Your changes to this draft are lost."], action: "Start over", danger: true };
    if (untouched || (await confirm(ask))) edit(starter.yaml);
  }

  /** A version is saved: by the save button, or by the planner when asked to. */
  function saved(version: WorkflowVersion, yaml: string) {
    setSavedText(yaml);
    unsaved.current = false;
    setNotice(`Saved as version ${version.number}; runs already started keep their version.`);
    notify(`Saved as version ${version.number}.`, { tone: "ok" });
    if (!workflowId) {
      firstSave.current = version.workflow_id;
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

  // Still on its way; a load that failed says so in its alert instead.
  const loading = text === null && !error;
  // The parsed YAML names it first, so the title is right before the check returns.
  const title =
    asText(parsed?.doc.name).trim() || model?.name?.trim() || (workflowId ? "Workflow" : "New workflow");
  const schedule = typeof parsed?.doc.schedule === "string" ? parsed.doc.schedule : "";
  const steps = parsed?.stages ?? [];
  const current = Math.min(selected, Math.max(steps.length - 1, 0));
  // The UI reads the stage package's declaration, never its name (ADR 0025).
  const stage = steps[current];
  const called = stage && stageType(stage) === "connector" ? connectors[asText(stage.calls)] : undefined;
  const held = called?.templates;
  const offered = held ? templates.filter((t) => t.folder === held) : null;
  // a build starts from its Template (`base`), a direct render reads one in place (`project`)
  const param = called?.capabilities.includes("build_session") ? "base" : "project";
  const tabs: [EditorTab, string][] = [
    ["stages", "Stages"],
    ["planner", role("planner")],
    ["yaml", "YAML"],
    ...(workflowId ? ([["scorecard", "Scorecard"]] as [EditorTab, string][]) : []),
  ];
  const problems = violations?.length ?? 0;
  const status = removed
    ? `Removed ${removed}.`
    : checking
      ? "Checking..."
      : notice ??
        (loading
          ? "" // the save button says it
          : !dirty
            ? "Everything here is saved."
            : problems > 0
              ? `${problems} ${problems === 1 ? "problem" : "problems"} to fix`
              : clean
                ? "Unsaved changes. Checks pass."
                : "Unsaved changes.");
  const panel = view === "stages" && parsed && stage && (
    <StagePanel
      stages={steps}
      index={current}
      agents={asText(parsed.doc.agents) || "1"}
      templates={offered}
      param={param}
      phone={phone}
      onField={setField}
      onBase={(index, base) => setBase(index, param, base)}
      onMove={move}
      onRemove={remove}
      onDone={() => setSheet(false)}
    />
  );

  return (
    <main className="editor-page">
      {dialog}
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/worlds/$worldId/workflows" params={{ worldId }}>
              Workflows
            </Link>
            {!loading && ` / ${title}`}
          </div>
          {loading && workflowId ? (
            <Loading>
              <h1>
                <Skeleton w={240} h="1.2em" />
              </h1>
            </Loading>
          ) : (
            <h1>
              {title} {versions.length > 0 && <span className="faint-title">v{versions[versions.length - 1]?.number}</span>}
            </h1>
          )}
          {!loading && (
            <p className="sub">
              {steps.length > 0
                ? `${steps.length} ${steps.length === 1 ? "step" : "steps"}: ${steps.map((s, i) => stageName(s, i)).join(", ")}.`
                : "No steps yet."}
            </p>
          )}
        </div>
        {/* On a phone this is the save bar pinned above the tab bar. */}
        <div className="editor-actions action-bar" data-testid="save-bar">
          <span className="save-status">
            <span className="faint" role="status" data-testid="check-status">
              {status}
            </span>
            {removed && (
              <button type="button" className="link undo" onClick={undo}>
                Undo
              </button>
            )}
          </span>
          <button type="button" className="primary" disabled={!canSave} onClick={save}>
            {busy ? "Saving..." : loading ? "Save" : `Save as version ${nextVersion}`}
          </button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {!workflowId && starters.length > 0 && (
        <section className="starters" aria-label="Starter workflows">
          {starters.map((s) => (
            <button key={s.name} type="button" aria-pressed={text === s.yaml} onClick={() => begin(s)}>
              <b>{s.name}</b>
              <small>{s.about}</small>
            </button>
          ))}
        </section>
      )}
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
          <div hidden={view !== "planner"}>
            <PlannerChat
              worldId={worldId}
              workflowId={workflowId}
              text={text}
              onDraft={(yaml) => {
                setText(yaml);
                setNotice(`${role("planner")} updated the draft. Review it in Stages or YAML, then save.`);
              }}
              onSaved={saved}
            />
          </div>
          {view === "stages" &&
            (loading ? (
              <StagesLoading />
            ) : parsed ? (
              <StageList
                doc={parsed.doc}
                stages={parsed.stages}
                selected={current}
                nextRunAt={nextRunAt}
                phone={phone}
                onSelect={phone ? open : setSelected}
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
                autoCapitalize="off"
                autoCorrect="off"
                wrap="off"
                rows={22}
                onChange={(e) => edit(e.target.value)}
              />
              <p className="faint">Comments typed here are dropped when you edit in Stages.</p>
            </>
          )}
          {view === "scorecard" && workflowId && <Scorecard worldId={worldId} workflowId={workflowId} />}
          {view !== "scorecard" && <Problems violations={violations} checking={checking} />}
          {view !== "scorecard" && <Warnings warnings={warnings} />}
        </section>
        <aside>
          {!phone && panel}
          <section aria-label="Versions">
            <h2>Versions</h2>
            {!workflowId ? (
              <p className="faint">Version 1 is created when you save.</p>
            ) : loading ? (
              <Skeleton lines={2} />
            ) : (
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
            <CopyTo worlds={worlds.filter((w) => w.id !== worldId)} copied={copied} phone={phone} onCopy={copyTo} />
          )}
        </aside>
      </div>
      {phone && (
        <Sheet open={sheet && !!panel} onClose={() => setSheet(false)} label="Stage settings" className="stage-sheet">
          {panel}
        </Sheet>
      )}
    </main>
  );
}

/** The Stages view while the workflow loads: the Workflow fields and five stage rows, at size. */
function StagesLoading() {
  return (
    <Loading className="stages-loading">
      <div className="fields">
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} h={62} className={i < 2 ? "wide" : ""} />
        ))}
      </div>
      <div className="stage-rows">
        {Array.from({ length: 5 }, (_, i) => (
          <Skeleton key={i} h={58} />
        ))}
      </div>
    </Loading>
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

// Saves, but likely does not do what was meant, like a pick gate with one card to pick.
function Warnings({ warnings }: { warnings: Violation[] }) {
  if (warnings.length === 0) return null;
  return (
    <section aria-label="Warnings" data-testid="warnings">
      <h2>
        {warnings.length} {warnings.length === 1 ? "warning" : "warnings"}: it saves, but check it
      </h2>
      <ul className="problems">
        {warnings.map((v, i) => (
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
  phone,
  onSelect,
  onTop,
  onAdd,
}: {
  doc: Doc;
  stages: RawStage[];
  selected: number;
  nextRunAt: string | null;
  /** A phone adds with one chip per type and opens each stage in a sheet. */
  phone: boolean;
  onSelect: (index: number) => void;
  onTop: (key: string, value: unknown) => void;
  onAdd: (type: StageType) => void;
}) {
  const [adding, setAdding] = useState<StageType>("swarm");
  return (
    <div data-testid="stage-list">
      <div className="fields" role="group" aria-labelledby="workflow-fields">
        <h2 id="workflow-fields" className="fields-title">
          Workflow
        </h2>
        <label className="wide">
          <span>Name</span>
          <input value={asText(doc.name)} onChange={(e) => onTop("name", e.target.value)} />
        </label>
        <div className="wide">
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
            inputMode="decimal"
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
            inputMode="numeric"
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
              {phone && (
                <Icon name="next" className="chev" />
              )}
            </button>
          </li>
        ))}
      </ol>
      {phone ? (
        <div className="add-stage" role="group" aria-label="Add a stage">
          {STAGE_TYPES.map((t) => (
            <button key={t} type="button" className="add-chip" onClick={() => onAdd(t)}>
              + {t[0]?.toUpperCase()}
              {t.slice(1)}
            </button>
          ))}
        </div>
      ) : (
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
      )}
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
  templates,
  param,
  phone,
  onField,
  onBase,
  onMove,
  onRemove,
  onDone,
}: {
  stages: RawStage[];
  index: number;
  /** The workflow's agents per swarm stage, what a stage without its own runs. */
  agents: string;
  /** The Templates of the stage's package, when it declares them; null when it does not. */
  templates: Template[] | null;
  /** The param the Template is written to: `base` for a build, `project` for a direct render. */
  param: string;
  /** In a phone's sheet: a Done button, the gate's kind as segments, one field per row. */
  phone: boolean;
  onField: (index: number, key: string, value: unknown) => void;
  onBase: (index: number, base: string) => void;
  onMove: (index: number, by: -1 | 1) => void;
  onRemove: (index: number) => void;
  onDone: () => void;
}) {
  const stage = stages[index] ?? {};
  const type = stageType(stage);
  const name = stageName(stage, index);
  const { needs, makes } = contract(stages, index);
  return (
    <section aria-label="Stage settings" className="stage-panel" data-testid="stage-panel">
      <div className="row stage-head">
        <h2>{name}</h2>
        <span className={`pill type-${type ?? "unknown"}`}>{type ?? "unknown type"}</span>
        {phone && (
          <button type="button" className="done" onClick={onDone}>
            Done
          </button>
        )}
      </div>
      {type ? (
        <>
          <label>
            <span>Name</span>
            <input aria-label={`Name of stage ${index + 1}`} value={name} onChange={(e) => onField(index, type, e.target.value)} />
          </label>
          {FIELDS[type].map((f) =>
            f.kind === "gate_kind" && phone ? (
              <fieldset key={f.key} className="segments">
                <legend>{f.label}</legend>
                {GATE_KINDS.map((k) => (
                  <label key={k}>
                    <input
                      type="radio"
                      name={`kind-${index}`}
                      value={k}
                      checked={(asText(stage[f.key]) || "approve") === k}
                      onChange={() => onField(index, f.key, k)}
                    />
                    <span>{k}</span>
                  </label>
                ))}
              </fieldset>
            ) : (
            <label key={f.key}>
              <span>{f.label}</span>
              {f.kind === "gate_kind" ? (
                <select
                  aria-label={`${f.label} of ${name}`}
                  value={asText(stage[f.key]) || "approve"}
                  onChange={(e) => onField(index, f.key, e.target.value)}
                >
                  {GATE_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              ) : f.kind === "count" ? (
                <input
                  aria-label={`${f.label} of ${name}`}
                  type="number"
                  inputMode="numeric"
                  min={1}
                  step={1}
                  placeholder={agents}
                  value={asText(stage[f.key])}
                  onChange={(e) => onField(index, f.key, e.target.value === "" ? "" : Number(e.target.value))}
                />
              ) : f.kind === "long" ? (
                <textarea
                  aria-label={`${f.label} of ${name}`}
                  rows={phone ? 4 : 3}
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
            ),
          )}
          {templates && (
            <TemplatePicker
              name={name}
              templates={templates}
              starter={param === "base"}
              base={asText((stage.params as Doc | undefined)?.[param])}
              onBase={(b) => onBase(index, b)}
            />
          )}
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
      <span className="row-start stage-moves">
        <button type="button" aria-label={`Move ${name} up`} disabled={index === 0} onClick={() => onMove(index, -1)}>
          {phone ? "Move up" : "Up"}
        </button>
        <button
          type="button"
          aria-label={`Move ${name} down`}
          disabled={index === stages.length - 1}
          onClick={() => onMove(index, 1)}
        >
          {phone ? "Move down" : "Down"}
        </button>
        <button type="button" className="danger" aria-label={`Remove ${name}`} onClick={() => onRemove(index)}>
          Remove
        </button>
      </span>
    </section>
  );
}

/** The Template a connector stage starts from, the package's starter unless one is picked; a
 * direct render, which has no `starter`, must pick one. */
function TemplatePicker({
  name,
  templates,
  starter,
  base,
  onBase,
}: {
  name: string;
  templates: Template[];
  starter: boolean;
  base: string;
  onBase: (base: string) => void;
}) {
  return (
    <label>
      <span>Template</span>
      <select aria-label={`Template of ${name}`} value={base} onChange={(e) => onBase(e.target.value)}>
        <option value="">{starter ? "The package's starter" : "Pick a Template"}</option>
        {templates.map((t) => (
          <option key={t.path} value={t.name}>
            {t.name}
          </option>
        ))}
        {base && !templates.some((t) => t.name === base) && <option value={base}>{base} (not in this world)</option>}
      </select>
    </label>
  );
}

type ChatLine = { who: "you" | "planner"; text: string };

/** The planner: one message in, a reply and often a whole new draft out. */
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
      setFailed(typeof detail === "string" ? `${detail} Send it again.` : `${role("planner")} did not answer (${response.status}). Send it again.`);
      setMessage(said);
      return;
    }
    setSessionId(data.session_id);
    setLines((prev) => [...prev, { who: "planner", text: data.reply }]);
    if (data.saved) onSaved(data.saved, data.saved.yaml);
    else if (data.yaml) onDraft(data.yaml);
  }

  return (
    <div className="chat" data-testid="planner-chat">
      {lines.length === 0 && (
        <p className="faint">
          Tell {role("planner")} what the workflow should do, in plain words. You get back a stage
          list in the editor, with questions only about what changes its shape. Ask for a save
          when you are happy with it.
        </p>
      )}
      <ul className="chat-log" data-testid="chat-log">
        {lines.map((l, i) => (
          <li key={i} className={l.who}>
            <b>{l.who === "you" ? "You" : role("planner")}</b>
            {l.text}
          </li>
        ))}
        {thinking && (
          <li className="planner" role="status">
            <b>{role("planner")}</b>Thinking...
          </li>
        )}
      </ul>
      {failed && <p role="alert">{failed}</p>}
      <form onSubmit={send}>
        <textarea
          aria-label={`Message to ${role("planner")}`}
          rows={2}
          value={message}
          placeholder="Every Monday: what changed in my field, five findings with sources, and I approve the summary."
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
  phone,
  onCopy,
}: {
  worlds: World[];
  copied: { world: World; workflowId: string } | null;
  /** A phone picks the world in a sheet, not a menu under the button. */
  phone: boolean;
  onCopy: (world: World) => void;
}) {
  const [open, setOpen] = useState(false);
  const pick = (w: World) => {
    setOpen(false);
    onCopy(w);
  };
  return (
    <section aria-label="Copy" className="copy">
      {worlds.length === 0 ? (
        <p className="faint">Create a second world to copy this workflow into it.</p>
      ) : (
        <span className="rel">
          <button type="button" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}>
            Copy to another world
          </button>
          {open && !phone && (
            <span className="menu" role="menu">
              {worlds.map((w) => (
                <button key={w.id} type="button" role="menuitem" onClick={() => pick(w)}>
                  {w.name}
                  <small>Creates a copy there; you stay here</small>
                </button>
              ))}
            </span>
          )}
        </span>
      )}
      {phone && (
        <Sheet open={open} onClose={() => setOpen(false)} label="Copy to another world">
          <h2 className="sheet-title">Copy to another world</h2>
          <p className="faint">Creates a copy there; you stay here.</p>
          {worlds.map((w) => (
            <button key={w.id} type="button" className="sheet-row" onClick={() => pick(w)}>
              {w.name}
            </button>
          ))}
        </Sheet>
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
