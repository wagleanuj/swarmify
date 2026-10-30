import { Link, useRouter } from "@tanstack/react-router";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "../api/client";
import { labelOf, useConnectors } from "../api/connectors";
import { dropGate, useOpenGates } from "../api/gates";
import { type Agent, type ArtifactFile, type LogEntry, type Verdict, useWorldEvents } from "../api/events";
import { actOn, type Call, current, duration, type Gate, money, type Run, STATUS_LABEL, statusLine, took } from "../api/runs";
import type { components } from "../api/schema";
import { type Task, upsert } from "../api/tasks";
import { runRoute } from "../router";
import { Sheet } from "../Sheet";
import { the } from "../skin";
import { type Answer, GateCard } from "./Gates";
import { RunRef, version } from "./Runs";
import { type MenuItem, Menu } from "./Menu";
import { kindOf, Result, resultStage, StagePanel, stepMeta } from "./StagePanel";
import { TimeStrip } from "./TimeStrip";

type World = components["schemas"]["World"];
type Stage = Run["stages"][number];

/** The step the run is at now: the one waiting for you, else the one working. */
function liveStage(run: Run): Stage | undefined {
  return run.stages.find((s) => s.status === "waiting") ?? run.stages.find((s) => s.status === "running");
}

/** The step picked on arrival when the URL names none: the live one, else for a done run the
 * step that made its result, else the last finished, else the first. It is pinned in the URL
 * then, so it never moves by itself. Undefined while a done run's artifacts are loading. */
function arrivalStage(run: Run, artifacts: ArtifactFile[] | null): Stage | undefined {
  const live = liveStage(run);
  if (!live && run.status === "done") {
    if (!artifacts) return undefined;
    const made = resultStage(run, artifacts);
    if (made) return made;
  }
  return live ?? run.stages.filter((s) => s.status === "done" || s.status === "failed").at(-1) ?? run.stages[0];
}

export function RunTimeline() {
  const { worldId, runId } = runRoute.useParams();
  const search = runRoute.useSearch();
  const router = useRouter();
  const [run, setRun] = useState<Run | null | undefined>(undefined);
  const [world, setWorld] = useState<World | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [calls, setCalls] = useState<Record<string, Call | null>>({});
  const [gate, setGate] = useState<Gate | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Record<string, Agent>>({});
  const [log, setLog] = useState<LogEntry[]>([]);
  const [verdicts, setVerdicts] = useState<Verdict[]>([]);
  const [loaded, setArtifacts] = useState<ArtifactFile[] | null>(null);
  const artifacts = loaded ?? [];
  const [saved, setSaved] = useState<string | null>(null);
  const [artifactsSeen, setArtifactsSeen] = useState(0);
  const [baseline, setBaseline] = useState<Run | null>(null);
  // The gate answered here last: its step shows what you decided until you move on yourself.
  const [answered, setAnswered] = useState<{ stageId: string; said: string; at: string } | null>(null);
  const [stepsOpen, setStepsOpen] = useState(false);
  // The shell's top bar, where a phone shows "‹ Runs" and the run menu.
  const [topbar, setTopbar] = useState<Element | null>(null);
  useEffect(() => setTopbar(document.getElementById("topbar-slot")), []);
  const openGates = useOpenGates().gates ?? [];
  const connectors = useConnectors(worldId);
  const chosen = run?.stages.find((s) => s.id === search.stage);
  const stage = run ? (chosen ?? arrivalStage(run, loaded)) : undefined;
  const stageId = stage?.id;
  const live = run ? liveStage(run) : undefined;
  const swarmId = stage?.type === "swarm" ? stage.id : undefined;
  const [, tick] = useState(0);
  const moving = run?.status === "running";
  // The status line's "so far" and the running step's time move on between events.
  useEffect(() => {
    if (!moving) return;
    const timer = setInterval(() => tick((n) => n + 1), 15_000);
    return () => clearInterval(timer);
  }, [moving]);
  const waitingAt = run?.status === "waiting_gate" ? `${current(run)?.id}:${run.send_backs}:${run.over_budget}` : null;

  useEffect(() => {
    api
      .GET("/api/runs/{run_id}", { params: { path: { run_id: runId } } })
      .then(({ data, response }) => {
        if (response.status === 404) return setRun(null);
        if (!data) throw new Error("load failed");
        setRun(data);
      })
      .catch(() => setError("Could not load this run. Is the API running?"));
  }, [runId]);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorld(data ?? null))
      .catch(() => undefined);
  }, [worldId]);

  useEffect(() => {
    api
      .GET("/api/runs/{run_id}/baseline", { params: { path: { run_id: runId } } })
      .then(({ data }) => setBaseline(data ?? null))
      .catch(() => undefined);
  }, [runId]);

  useEffect(() => {
    api
      .GET("/api/runs/{run_id}/agents", { params: { path: { run_id: runId } } })
      .then(({ data }) => data && setAgents((prev) => ({ ...Object.fromEntries(data.map((a) => [a.id, a])), ...prev })))
      .catch(() => undefined);
  }, [runId]);

  useEffect(() => {
    api
      .GET("/api/runs/{run_id}/artifacts", { params: { path: { run_id: runId } } })
      .then(({ data }) => setArtifacts((prev) => data ?? prev ?? []))
      .catch(() => setArtifacts((prev) => prev ?? []));
  }, [runId, artifactsSeen]);

  // The open gate, so the status line can say what it asks and answer it here.
  useEffect(() => {
    if (!waitingAt) return setGate(null);
    api
      .GET("/api/gates")
      .then(({ data }) => setGate((data ?? []).find((g) => g.run_id === runId) ?? null))
      .catch(() => undefined);
  }, [runId, waitingAt]);

  useEffect(() => {
    setLog([]);
    if (!stageId) return;
    let stale = false; // a slow answer for the step selected before must not land here
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/log", { params: { path: { run_id: runId, stage_id: stageId } } })
      .then(({ data }) => data && !stale && setLog((prev) => dedupe(data, prev, "seq")))
      .catch(() => undefined);
    return () => {
      stale = true;
    };
  }, [runId, stageId]);

  useEffect(() => {
    if (!stageId || stage?.type !== "connector" || calls[stageId] !== undefined) return;
    api
      .GET("/api/stages/{stage_id}/call", { params: { path: { stage_id: stageId } } })
      .then(({ data }) => setCalls((prev) => ({ ...prev, [stageId]: prev[stageId] ?? data ?? null })))
      .catch(() => undefined);
  }, [stageId, stage?.type, stage?.status, calls]);

  useEffect(() => {
    setVerdicts([]);
    if (!swarmId) return;
    let stale = false;
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/verdicts", { params: { path: { run_id: runId, stage_id: swarmId } } })
      .then(({ data }) => data && !stale && setVerdicts((prev) => dedupe(data, prev, "loop")))
      .catch(() => undefined);
    return () => {
      stale = true;
    };
  }, [runId, swarmId]);

  useEffect(() => {
    setTasks([]);
    if (!swarmId) return;
    let stale = false;
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/tasks", { params: { path: { run_id: runId, stage_id: swarmId } } })
      .then(({ data }) => data && !stale && setTasks((prev) => data.reduce(upsert, prev)))
      .catch(() => setError("Could not load the whiteboard. Is the API running?"));
    return () => {
      stale = true;
    };
  }, [runId, swarmId]);

  useWorldEvents(worldId, {
    "run.updated": (update) => {
      if (update.id !== runId) return;
      // The stream replays history first: an older snapshot never replaces a newer one.
      setRun((prev) => (prev && prev.updated_at > update.updated_at ? prev : update));
      // A connector stage that just started or ended has a call to (re)load.
      setCalls((prev) => Object.fromEntries(Object.entries(prev).filter(([, c]) => c !== null)));
    },
    "task.updated": (task) => {
      if (task.stage_id === swarmId) setTasks((prev) => upsert(prev, task));
    },
    "agent.updated": (agent) => {
      if (agent.run_id === runId) setAgents((prev) => ({ ...prev, [agent.id]: agent }));
    },
    "agent.logged": (entry) => {
      if (entry.stage_id === stageId) setLog((prev) => dedupe(prev, [entry], "seq"));
    },
    "verdict.created": (verdict) => {
      if (verdict.stage_id === swarmId) setVerdicts((prev) => dedupe(prev, [verdict], "loop"));
    },
    "artifact.created": (created) => {
      if (created.type !== "note" && run?.stages.some((s) => s.id === created.stage_id)) setArtifactsSeen((n) => n + 1);
    },
    "connector_call.updated": (call) => {
      if (call.run_id === runId) setCalls((prev) => ({ ...prev, [call.idempotency_key]: call }));
    },
  });

  const select = useCallback(
    (id: string) => void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId }, search: { stage: id }, replace: true }),
    [router, worldId, runId],
  );

  // Arriving with no step (or one this run lacks) pins the picked one; nothing else writes it.
  useEffect(() => {
    if (stageId && !chosen) select(stageId);
  }, [stageId, chosen, select]);

  async function act(action: "pause" | "resume" | "abort") {
    if (!run) return;
    if (action === "abort" && !window.confirm(`Abort ${run.name}? Any child runs it started stop too. Finished stages keep their artifacts.`)) {
      return;
    }
    setError(null);
    const refused = await actOn(run.id, action);
    if (refused) setError(refused);
  }

  /** A Fork: a new run that keeps every stage before `index` and runs this one anew. */
  async function fork(index: number) {
    if (!run) return;
    setError(null);
    const { data, error, response } = await api.POST("/api/runs/{run_id}/fork", {
      params: { path: { run_id: run.id } },
      body: { stage: index },
    });
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      return setError(typeof detail === "string" ? detail : `Could not fork the run (${response.status}).`);
    }
    void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId: data.id } });
  }

  /** A done run becomes a workflow: the stages it ran, gates added by hand included. You stay
   * on the run; a link opens the new workflow. */
  async function saveAsWorkflow() {
    if (!run) return;
    setError(null);
    const { data, error, response } = await api.POST("/api/runs/{run_id}/workflow", {
      params: { path: { run_id: run.id } },
      body: {},
    });
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      return setError(typeof detail === "string" ? detail : `Could not save the run as a workflow (${response.status}).`);
    }
    setSaved(data.workflow_id);
  }

  const answer: Answer = async (g, send, said = "You answered it") => {
    setError(null);
    const refused = await send();
    if (refused) return setError(refused);
    if (g.kind === "budget") return;
    setGate(null);
    dropGate(g.stage_id);
    setAnswered({ stageId: g.stage_id, said, at: new Date().toISOString() });
  };

  if (run === undefined) {
    return (
      <main>
        {error ? <p role="alert">{error}</p> : <p role="status">Loading...</p>}
      </main>
    );
  }
  if (run === null) {
    return (
      <main>
        <h1>No such run</h1>
        <p className="sub">
          Its world may have been deleted.{" "}
          <Link to="/worlds/$worldId/runs" params={{ worldId }}>
            Back to runs.
          </Link>
        </p>
      </main>
    );
  }
  const ended = ["done", "failed", "aborted"].includes(run.status);
  const forkable = (index: number) =>
    run.stages.slice(0, index).every((s) => s.status === "done" || s.status === "skipped") &&
    ["done", "failed"].includes(run.stages[index]?.status ?? "");
  const post = artifacts.filter((a) => a.type === "publish").at(-1);
  const postLink = post ? Object.values((post.fields.links as Record<string, string> | undefined) ?? {})[0] : undefined;
  const made = stage ? artifacts.filter((a) => a.stage === stage.id) : [];
  const kind = (s: Stage) => kindOf(s, artifacts.filter((a) => a.stage === s.id), connectors[s.calls ?? ""]);
  const gateStage = gate ? run.stages.find((s) => s.id === gate.stage_id) : undefined;
  const open = gate && gate.stage_id === stageId ? gate : null;
  const done = !open && answered && answered.stageId === stageId ? answered : null;
  const isResult = !open && !done && !!stage && resultStage(run, artifacts)?.id === stage.id;
  const started = run.stages.find((s) => s.started_at)?.started_at;
  const seconds = duration(run);
  const items: MenuItem[] = [];
  if (!ended) {
    items.push(
      run.status === "paused"
        ? { label: "Resume", note: "Agents pick up where they stopped; you stay here", onSelect: () => act("resume") }
        : { label: "Pause", note: "Agents finish their current task, then stop; you stay here", disabled: run.status === "queued", onSelect: () => act("pause") },
    );
  }
  if (stage && forkable(stage.index)) {
    const at = stage;
    items.push({
      label: `Fork from ${at.name}`,
      note: `Opens a new run that keeps every step before ${at.name}; this one stays as it is`,
      onSelect: () => fork(at.index),
    });
  }
  if (run.status === "done") {
    items.push({ label: "Save as workflow", note: "Saves the steps it ran as a new workflow; you stay on this run", onSelect: saveAsWorkflow });
  }
  if (!ended) {
    items.push({ label: "Abort run", note: "Stops every agent; finished steps keep their artifacts", danger: true, onSelect: () => act("abort") });
  }
  /** The step list and spend: beside the step on a wide screen, in the steps sheet on a phone. */
  const side = (pick: (id: string) => void) => (
    <aside className="run-side">
      <nav className="panel steps" aria-label="Steps">
        <ol data-testid="timeline">
          {run.stages.map((s) => {
            const now = run.status === "queued" ? s.index === 0 : s.status === "running" || s.status === "waiting";
            const meta = stepMeta(s, run.status === "queued");
            return (
              <li key={s.id} data-status={s.status} className={`step step-${s.status}`}>
                <button
                  type="button"
                  aria-pressed={s.id === stageId}
                  aria-current={now ? "step" : undefined}
                  aria-label={`Step ${s.index + 1}, ${s.name}, ${meta}`}
                  onClick={() => pick(s.id)}
                >
                  <Glyph status={s.status} />
                  <span className="step-name">
                    {s.name}
                    <small>{s.status === "waiting" ? "waiting for you" : kind(s)}</small>
                  </span>
                  <span className="step-meta">{s.status === "pending" ? "-" : meta}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>
      <Spend run={run} baseline={baseline} />
    </aside>
  );
  return (
    <main className="run-page">
      <header className="page">
        <div>
          <h1>
            {run.name} <span className="faint-title">{version(run)}</span>
          </h1>
          {/* On a phone the line leads with the run's state; waiting says it in amber instead. */}
          {run.status !== "waiting_gate" && (
            <span className={`status status-${run.status} run-pill`} data-testid="run-pill">
              {STATUS_LABEL[run.status]}
            </span>
          )}
          <p role="status" data-testid="status-line" className={`status-line state-${run.status}`}>
            {statusLine(run, gate?.ask, connectors)}
            {run.send_backs > 0 && ` Sent back ${run.send_backs === 1 ? "once" : `${run.send_backs} times`}.`}
            {run.status === "done" && post && ` Posted to ${labelOf(connectors, String(post.fields.connector))}.`}
          </p>
          <Lineage run={run} worldId={worldId} />
        </div>
        <div className="row run-actions" data-testid="run-actions">
          {live && live.id !== stageId && (
            <span className="follow" data-testid="follow">
              Now at {live.name} ·{" "}
              <button type="button" onClick={() => select(live.id)}>
                Go there
              </button>
            </span>
          )}
          {gateStage && gateStage.id !== stageId && (
            <button type="button" className="primary" onClick={() => select(gateStage.id)}>
              Answer {gateStage.name}
            </button>
          )}
          {run.status === "done" && postLink && (
            <a href={postLink} target="_blank" rel="noreferrer" className="button">
              Open the post
            </a>
          )}
          {items.length > 0 && (
            <Menu label="Run actions" className="icon" button={<MoreIcon />} items={items} />
          )}
        </div>
      </header>
      {topbar &&
        createPortal(
          <div className="run-topbar">
            <Link to="/worlds/$worldId/runs" params={{ worldId }}>
              ‹ Runs
            </Link>
            {items.length > 0 && <Menu label="Run actions" className="icon" button={<MoreIcon />} items={items} />}
          </div>,
          topbar,
        )}
      {error && <p role="alert">{error}</p>}
      {saved && (
        <p className="saved" role="status" data-testid="saved">
          Saved as a workflow.{" "}
          <Link to="/worlds/$worldId/workflows/$workflowId" params={{ worldId, workflowId: saved }}>
            Open it in the editor
          </Link>
        </p>
      )}
      <TimeStrip stages={run.stages} selected={stageId} onSelect={select} />
      <div className="strip-axis">
        <span>{started ? new Date(started).toLocaleTimeString([], { timeStyle: "short" }) : "Not started"}</span>
        {seconds !== null && <span className="axis-time">{ended ? took(seconds) : `${took(seconds)} so far`}</span>}
        {/* A phone has the time in the status line, so the strip carries the spend. */}
        <span className="axis-spend" data-testid="strip-spend">
          {run.budget_usd != null ? `${money(run.spent_usd ?? 0)} of ${money(run.budget_usd)}` : `${money(run.spent_usd ?? 0)} spent`}
        </span>
      </div>
      {stage && (
        <button type="button" className="step-button" aria-haspopup="dialog" aria-expanded={stepsOpen} onClick={() => setStepsOpen(true)}>
          <span>
            <small>
              Step {stage.index + 1} of {run.stages.length} · {kind(stage)}
            </small>
            <b>{stage.name}</b>
          </span>
          <small>All steps ▾</small>
        </button>
      )}
      <Sheet open={stepsOpen} onClose={() => setStepsOpen(false)} label="All steps">
        {side((id) => {
          setStepsOpen(false);
          select(id);
        })}
      </Sheet>
      <div className="run-grid">
        {side(select)}
        {stage && (
          <StagePanel
            run={run}
            stage={stage}
            worldId={worldId}
            made={made}
            artifacts={artifacts}
            agents={Object.values(agents).filter((a) => a.stage_id === stage.id)}
            log={log}
            tasks={tasks}
            verdicts={verdicts}
            call={calls[stage.id]}
            connectors={connectors}
            onTaskAdded={(task) => setTasks((prev) => upsert(prev, task))}
            lead={
              open ? (
                <ul className="gate-list inline-gate" aria-label="Answer the gate">
                  <GateCard gate={open} world={world?.name ?? worldId} onAnswer={answer} bare />
                </ul>
              ) : done && stage ? (
                <AnsweredLead
                  stage={stage}
                  said={done.said}
                  at={done.at}
                  live={live}
                  next={openGates.find((g) => g.stage_id !== done.stageId)}
                  onWatch={select}
                />
              ) : isResult ? (
                <Result run={run} artifacts={artifacts} worldId={worldId} />
              ) : undefined
            }
            answering={!!open}
            resultShown={isResult}
          />
        )}
      </div>
    </main>
  );
}

/** A gate just answered here: what you decided and when, where the run went, and the ways on.
 * The gate's record below keeps your note. You stay on this step until you pick a way on. */
function AnsweredLead(p: {
  stage: Stage;
  said: string;
  at: string;
  live: Stage | undefined;
  next: Gate | undefined;
  onWatch: (id: string) => void;
}) {
  const { stage, live, next } = p;
  const moved = live && live.id !== stage.id ? live : undefined;
  const when = new Date(p.at).toLocaleTimeString([], { timeStyle: "short" });
  return (
    <div className="answered" data-testid="answered">
      <p className="notice ok" role="status">
        {p.said} at {when}.
        {moved && (moved.index < stage.index ? ` ${moved.name} is running again.` : ` The run moved on to ${moved.name}.`)}
      </p>
      <div className="answer-bar">
        {next && (
          <Link to="/gates" search={{ gate: next.stage_id }} className="button primary">
            Next decision: {next.stage_name} on {next.run_name}
          </Link>
        )}
        {moved && (
          <button type="button" onClick={() => p.onWatch(moved.id)}>
            Watch {moved.name}
          </button>
        )}
        <span className="faint">You stay on this step until you choose.</span>
      </div>
    </div>
  );
}

/** Where this run came from and what came of it: parent, fork source, library pick, forks. */
function Lineage({ run, worldId }: { run: Run; worldId: string }) {
  const lines: ReactNode[] = [];
  if (run.parent) {
    lines.push(
      <p className="sub" key="parent">
        <RunRef label="child of" run={run.parent} testId="child-of" />. It spends that run's budget, and aborting that run aborts this one.
      </p>,
    );
  }
  if (run.forked_from) {
    lines.push(
      <p className="sub lineage" data-testid="forked-from" key="fork">
        <RunRef label="Forked from" run={run.forked_from} />
        {run.fork_stage_index != null && ` at stage ${run.fork_stage_index + 1}. The earlier stages were copied, not run again.`}
      </p>,
    );
  }
  if (run.promoted_from) {
    lines.push(
      <p className="sub lineage" data-testid="promoted-from" key="promoted">
        Promoted from <code>{run.promoted_from.split("/").pop()}</code> in the{" "}
        <Link to="/worlds/$worldId/library" params={{ worldId }}>
          library
        </Link>
        ; the skipped stages never ran.
      </p>,
    );
  }
  if ((run.forks ?? []).length > 0) {
    lines.push(
      <p className="sub lineage" data-testid="forks" key="forks">
        Forks:{" "}
        {(run.forks ?? []).map((f, n) => (
          <span key={f.id}>
            {n > 0 && ", "}
            <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: f.world_id, runId: f.id }}>
              {f.name}
            </Link>
          </span>
        ))}
      </p>,
    );
  }
  return <>{lines}</>;
}

/** Part 5: what the run spent against its budget, what is left before the pause, and a link to
 * the one-agent comparison. */
function Spend({ run, baseline }: { run: Run; baseline: Run | null }) {
  const budget = run.budget_usd;
  const spent = run.spent_usd ?? 0;
  let note = "";
  if (budget != null && run.over_budget) note = `Every agent stopped at ${money(budget)}. Add money to go on, or abort.`;
  else if (budget != null) note = `${money(Math.max(0, budget - spent))} left before ${the("budget")} pauses the run.`;
  const seconds = duration(run);
  let compare: string;
  if (baseline) {
    const base = duration(baseline);
    compare = `One agent with no graph did this for ${money(baseline.spent_usd)}${base === null ? "" : ` in ${took(base)}`}; this run ${money(spent)}${seconds === null ? "" : ` in ${took(seconds)}`}.`;
  } else compare = "No one-agent run of this yet. Start a baseline from the world page to compare.";
  return (
    <section className="panel spend" aria-label="Spend" data-testid="measure">
      <div className="panel-head">
        <h3>Spend</h3>
        <span className="num" data-testid="budget">
          {budget != null ? (
            <>
              {money(spent)} <span className="faint">of {money(budget)}</span>
            </>
          ) : (
            `${money(spent)} spent, no budget set`
          )}
        </span>
      </div>
      {budget != null && (
        <meter min={0} max={budget} low={0.8 * budget} high={0.8 * budget} optimum={0} value={Math.min(spent, budget)} aria-label="Budget spent" />
      )}
      {note && (
        <span className={run.over_budget ? "stage-error" : budget != null && spent >= 0.8 * budget ? "warn" : "faint"} data-testid="budget-line">
          {note}
        </span>
      )}
      {run.baseline ? (
        <span className="faint" data-testid="baseline">
          This is a one-agent baseline, with no graph, to measure the swarm against.
        </span>
      ) : (
        <details className="compare">
          <summary>Compare with one agent</summary>
          <p className="faint" data-testid="baseline">
            {compare}
          </p>
        </details>
      )}
    </section>
  );
}

/** A step's state at a glance: a check when done, else a dot coloured by its state. */
function Glyph({ status }: { status: string }) {
  if (status === "done") {
    return (
      <svg className="glyph" width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3 8.5l3.2 3L13 4.5" />
      </svg>
    );
  }
  return <span className={`glyph dot dot-${status}`} aria-hidden="true" />;
}

function MoreIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
      <circle cx="3.5" cy="8" r="1.25" />
      <circle cx="8" cy="8" r="1.25" />
      <circle cx="12.5" cy="8" r="1.25" />
    </svg>
  );
}

/** Items of both lists by a numeric key, each once (the later wins), oldest first. */
function dedupe<T, K extends keyof T>(a: T[], b: T[], key: K): T[] {
  const byKey = new Map([...a, ...b].map((x) => [x[key], x]));
  return [...byKey.values()].sort((x, y) => Number(x[key]) - Number(y[key]));
}

