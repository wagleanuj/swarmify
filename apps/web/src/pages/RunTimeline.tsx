import { Link, useRouter } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { type Agent, type ArtifactFile, type LogEntry, type Verdict, useWorldEvents } from "../api/events";
import { actOn, type Call, decisionLine, duration, money, type Run, statusLine, took } from "../api/runs";
import { type Task, upsert } from "../api/tasks";
import { runRoute } from "../router";
import { AgentLog, usd } from "./AgentLog";
import { RunRef, version } from "./Runs";
import { StageOutput } from "./StageOutput";
import { Whiteboard } from "./Whiteboard";

/** The swarm stage whose whiteboard opens first: the live one, else the next, else the first. */
function defaultSwarm(run: Run) {
  const swarms = run.stages.filter((s) => s.type === "swarm");
  return (
    swarms.find((s) => s.status === "running") ??
    swarms.find((s) => s.status === "pending") ??
    swarms[0]
  );
}

export function RunTimeline() {
  const { worldId, runId } = runRoute.useParams();
  const router = useRouter();
  const [run, setRun] = useState<Run | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [calls, setCalls] = useState<Record<string, Call | null>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Record<string, Agent>>({});
  const [log, setLog] = useState<LogEntry[]>([]);
  const [verdicts, setVerdicts] = useState<Verdict[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactFile[]>([]);
  const [artifactsSeen, setArtifactsSeen] = useState(0);
  const [baseline, setBaseline] = useState<Run | null>(null);
  const stage = run ? (run.stages.find((s) => s.id === picked) ?? defaultSwarm(run)) : undefined;
  const stageId = stage?.type === "swarm" ? stage.id : undefined;

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
    setLog([]);
    if (!stageId) return;
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/log", {
        params: { path: { run_id: runId, stage_id: stageId } },
      })
      .then(({ data }) => data && setLog((prev) => dedupe(data, prev, "seq")))
      .catch(() => undefined);
  }, [runId, stageId]);

  useEffect(() => {
    setVerdicts([]);
    if (!stageId) return;
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/verdicts", {
        params: { path: { run_id: runId, stage_id: stageId } },
      })
      .then(({ data }) => data && setVerdicts((prev) => dedupe(data, prev, "loop")))
      .catch(() => undefined);
  }, [runId, stageId]);

  useEffect(() => {
    if (!stageId) return setArtifacts([]);
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/artifacts", {
        params: { path: { run_id: runId, stage_id: stageId } },
      })
      .then(({ data }) => data && setArtifacts(data))
      .catch(() => undefined);
  }, [runId, stageId, artifactsSeen]);

  useEffect(() => {
    setTasks([]);
    if (!stageId) return;
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/tasks", {
        params: { path: { run_id: runId, stage_id: stageId } },
      })
      .then(({ data }) => data && setTasks((prev) => data.reduce(upsert, prev)))
      .catch(() => setError("Could not load the whiteboard. Is the API running?"));
  }, [runId, stageId]);

  useWorldEvents(worldId, {
    "run.updated": (update) => {
      if (update.id === runId) setRun(update);
    },
    "task.updated": (task) => {
      if (task.stage_id === stageId) setTasks((prev) => upsert(prev, task));
    },
    "agent.updated": (agent) => {
      if (agent.run_id === runId) setAgents((prev) => ({ ...prev, [agent.id]: agent }));
    },
    "agent.logged": (entry) => {
      if (entry.stage_id === stageId) setLog((prev) => dedupe(prev, [entry], "seq"));
    },
    "verdict.created": (verdict) => {
      if (verdict.stage_id === stageId) setVerdicts((prev) => dedupe(prev, [verdict], "loop"));
    },
    "artifact.created": (created) => {
      if (created.stage_id === stageId && created.type !== "note") setArtifactsSeen((n) => n + 1);
    },
    "connector_call.updated": (call) => {
      if (call.run_id === runId) setCalls((prev) => ({ ...prev, [call.idempotency_key]: call }));
    },
  });

  function toggle(stageId: string) {
    setOpen((prev) => (prev === stageId ? null : stageId));
    if (calls[stageId]) return;
    api
      .GET("/api/stages/{stage_id}/call", { params: { path: { stage_id: stageId } } })
      .then(({ data }) => setCalls((prev) => ({ ...prev, [stageId]: prev[stageId] ?? data ?? null })))
      .catch(() => undefined);
  }

  async function act(action: "pause" | "resume" | "abort") {
    if (!run) return;
    if (
      action === "abort" &&
      !window.confirm(
        `Abort ${run.name}? Any child runs it started stop too. Finished stages keep their artifacts.`,
      )
    ) {
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

  /** A done run becomes a workflow: the stages it ran, gates added by hand included. */
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
    void router.navigate({
      to: "/worlds/$worldId/workflows/$workflowId",
      params: { worldId, workflowId: data.workflow_id },
    });
  }

  const crumb = (
    <div className="crumb">
      <Link to="/">worlds</Link> /{" "}
      <Link to="/worlds/$worldId/runs" params={{ worldId }}>
        runs
      </Link>
    </div>
  );
  if (run === undefined) {
    return (
      <main>
        {crumb}
        {error ? <p role="alert">{error}</p> : <p role="status">Loading...</p>}
      </main>
    );
  }
  if (run === null) {
    return (
      <main>
        {crumb}
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
  const decisionAt = (index: number) =>
    (run.decisions ?? []).filter((d) => d.stage_index === index).at(-1);
  // A stage can be forked from once it and every stage before it have finished.
  const forkable = (index: number) =>
    run.stages.slice(0, index + 1).every((s) => s.status === "done" || s.status === "skipped") &&
    run.stages[index]?.status === "done";
  return (
    <main>
      <header className="page">
        <div>
          {crumb}
          <h1>
            {run.name} <span className="faint-title">{version(run)}</span>
          </h1>
          {run.parent && (
            <p className="sub">
              <RunRef label="child of" run={run.parent} testId="child-of" />. It spends that run's budget, and aborting that run
              aborts this one.
            </p>
          )}
          {run.forked_from && (
            <p className="sub lineage" data-testid="forked-from">
              <RunRef label="Forked from" run={run.forked_from} />
              {run.fork_stage_index != null && ` at stage ${run.fork_stage_index + 1}. The earlier stages were copied, not run again.`}
            </p>
          )}
          {run.promoted_from && (
            <p className="sub lineage" data-testid="promoted-from">
              Promoted from <code>{run.promoted_from.split("/").pop()}</code> in the{" "}
              <Link to="/worlds/$worldId/library" params={{ worldId }}>
                library
              </Link>
              ; the skipped stages never ran.
            </p>
          )}
          {(run.forks ?? []).length > 0 && (
            <p className="sub lineage" data-testid="forks">
              Forks:{" "}
              {(run.forks ?? []).map((f, n) => (
                <span key={f.id}>
                  {n > 0 && ", "}
                  <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: f.world_id, runId: f.id }}>
                    {f.name}
                  </Link>
                </span>
              ))}
            </p>
          )}
          <p className="sub" role="status" data-testid="status-line">
            {statusLine(run)}
            {run.send_backs > 0 && ` Sent back ${run.send_backs} of 2 times.`}
          </p>
          {run.spent_usd > 0 && (
            <p className="faint" data-testid="run-cost">
              Estimated agent cost so far: {usd(run.spent_usd)}
              {Object.values(agents).some((a) => a.run_id !== run.id) && ", child runs included"}. An
              estimate from the Agent SDK, not your bill.
            </p>
          )}
          <Measure run={run} baseline={baseline} />
        </div>
        {run.status === "done" && (
          <div className="row" data-testid="run-actions">
            <button type="button" onClick={saveAsWorkflow}>
              Save as workflow
            </button>
          </div>
        )}
        {!ended && (
          <div className="row" data-testid="run-actions">
            {run.status === "waiting_gate" && (
              <Link to="/gates" className="button">
                {run.over_budget ? "Add money or abort" : "Answer the gate"}
              </Link>
            )}
            {run.status === "paused" ? (
              <button type="button" onClick={() => act("resume")}>
                Resume
              </button>
            ) : (
              <button type="button" onClick={() => act("pause")} disabled={run.status === "queued"}>
                Pause
              </button>
            )}
            <button type="button" className="danger" onClick={() => act("abort")}>
              Abort
            </button>
          </div>
        )}
      </header>
      {error && <p role="alert">{error}</p>}
      <ol className="timeline" aria-label="Timeline" data-testid="timeline">
        {run.stages.map((s) => {
          const call = calls[s.id];
          return (
            <li key={s.id} className={`stage stage-${s.status}`} data-status={s.status}>
              <span className="stage-head">
                {s.index + 1} · {s.status === "waiting" ? "you" : s.status} · {s.type}
              </span>
              <b>{s.name}</b>
              {call?.status === "polling" && (
                <span className="stage-progress">
                  {Math.round((call.progress ?? 0) * 100)}%{call.message ? ` · ${call.message}` : ""}
                </span>
              )}
              {s.child && (
                <span className="stage-note" data-testid="child-run">
                  Child run:{" "}
                  <Link
                    to="/worlds/$worldId/runs/$runId"
                    params={{ worldId: s.child.world_id, runId: s.child.id }}
                  >
                    {s.child.name}
                  </Link>
                </span>
              )}
              {s.error && <span className="stage-error">{s.error.split("\n")[0]}</span>}
              {decisionAt(s.index) && (
                <span className="stage-note">{decisionLine(decisionAt(s.index)!)}</span>
              )}
              {(s.notes ?? []).map((note, i) => (
                <span className="stage-note" key={i}>
                  Sent back with: {note}
                </span>
              ))}
              {s.type === "swarm" && (
                <button
                  type="button"
                  className="stage-pick"
                  aria-pressed={s.id === stageId}
                  aria-label={`Open the whiteboard of ${s.name}`}
                  onClick={() => setPicked(s.id)}
                >
                  whiteboard
                </button>
              )}
              {forkable(s.index) && (
                <button
                  type="button"
                  className="link"
                  aria-label={`Fork the run from ${s.name}`}
                  title="A new run that keeps everything before this stage and runs this one again"
                  onClick={() => fork(s.index)}
                >
                  Fork from here
                </button>
              )}
              {s.type === "connector" && (
                <button
                  type="button"
                  className="link"
                  aria-expanded={open === s.id}
                  onClick={() => toggle(s.id)}
                >
                  {open === s.id ? "Hide the call" : "Show the call"}
                </button>
              )}
            </li>
          );
        })}
      </ol>
      {failedLog(run)}
      {open && <CallPanel call={calls[open]} />}
      {stage && stageId && (
        <AgentLog agents={Object.values(agents).filter((a) => a.stage_id === stageId)} log={log} />
      )}
      {stage && stageId && <StageOutput verdicts={verdicts} artifacts={artifacts} />}
      {stage && stageId && (
        <Whiteboard
          runId={run.id}
          stage={stage}
          tasks={tasks}
          onAdded={(task) => setTasks((prev) => upsert(prev, task))}
        />
      )}
    </main>
  );
}

/** Russ (the budget) against the spend, and Hooli (the one-agent, no-graph baseline) beside
 * the swarm, as in the mockup's run tile. */
function Measure({ run, baseline }: { run: Run; baseline: Run | null }) {
  const budget = run.budget_usd;
  const spent = run.spent_usd ?? 0;
  const seconds = duration(run);
  const own = `This run: ${money(spent)}${seconds === null ? "" : ` and ${took(seconds)}`}`;
  let russ = "";
  if (budget != null) {
    if (run.over_budget) russ = `Russ stopped every agent at ${money(budget)}. Add money to go on, or abort.`;
    else if (spent >= 0.8 * budget) russ = `Past 80 percent. Russ stops every agent at ${money(budget)}.`;
    else russ = `Russ stops every agent at ${money(budget)}.`;
  }
  let hooli: string;
  if (run.baseline) hooli = "This is a Hooli baseline: one agent and no graph, to measure the swarm against.";
  else if (baseline) {
    const base = duration(baseline);
    hooli = `Hooli ran this alone for ${money(baseline.spent_usd)}${base === null ? "" : ` and ${took(base)}`}. ${own}.`;
  } else hooli = "No Hooli baseline yet: start one from the world page to measure this swarm against one agent with no graph.";
  return (
    <section className="measure" aria-label="Budget and baseline" data-testid="measure">
      {budget != null && (
        <>
          <span className="mono" data-testid="budget">
            Russ · {money(spent)} of {money(budget)}
          </span>
          <meter
            min={0}
            max={budget}
            low={0.8 * budget}
            high={0.8 * budget}
            optimum={0}
            value={Math.min(spent, budget)}
            aria-label="Budget spent"
          />
          <span className={run.over_budget ? "stage-error" : "faint"} data-testid="budget-line">
            {russ}
          </span>
        </>
      )}
      <span className="faint" data-testid="baseline">
        {hooli}
      </span>
    </section>
  );
}

/** Items of both lists by a numeric key, each once (the later wins), oldest first. */
function dedupe<T, K extends keyof T>(a: T[], b: T[], key: K): T[] {
  const byKey = new Map([...a, ...b].map((x) => [x[key], x]));
  return [...byKey.values()].sort((x, y) => Number(x[key]) - Number(y[key]));
}

/** A failed stage's error past its first line: what the connector printed, a CLI log say. */
function failedLog(run: Run) {
  const failed = run.stages.find((s) => s.status === "failed" && s.error?.includes("\n"));
  if (!failed?.error) return null;
  return (
    <section className="call" aria-label="Stage output">
      <h2>
        Stage {failed.index + 1}, {failed.name}: what it printed
      </h2>
      <pre className="stage-log" data-testid="stage-log">
        {failed.error.split("\n").slice(1).join("\n")}
      </pre>
    </section>
  );
}

/** A connector stage opened: what went in, what came out, and the raw call and response. */
function CallPanel({ call }: { call: Call | null | undefined }) {
  if (call === undefined) return <p role="status">Loading the call...</p>;
  if (call === null) {
    return (
      <section className="call" aria-label="Connector call" data-testid="call">
        <p className="faint">No call yet. It is recorded the moment this stage starts.</p>
      </section>
    );
  }
  const input = call.request.input as string | undefined;
  const output = call.request.output as string | undefined;
  return (
    <section className="call" aria-label="Connector call" data-testid="call">
      <h2>
        {call.connector} · <code>{call.tool}</code> <span className={`status status-${call.status}`}>{call.status}</span>
      </h2>
      <p className="faint">
        In: {input ?? "nothing"} · Out: {output ?? "nothing"} · attempt {call.attempts}
        {call.polls ? ` · polled ${call.polls} times` : ""} · key <code>{call.idempotency_key}</code>
      </p>
      {call.error && (
        <pre role="alert" className="stage-log">
          {call.error}
        </pre>
      )}
      <div className="call-json">
        <div>
          <h3>Request</h3>
          <pre data-testid="call-request">{JSON.stringify(call.request, null, 2)}</pre>
        </div>
        <div>
          <h3>Response</h3>
          <pre data-testid="call-response">{call.response ? JSON.stringify(call.response, null, 2) : "Waiting for an answer."}</pre>
        </div>
      </div>
    </section>
  );
}
