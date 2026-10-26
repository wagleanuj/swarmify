import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { type Agent, type ArtifactFile, type LogEntry, type Verdict, useWorldEvents } from "../api/events";
import { actOn, type Call, decisionLine, type Run, statusLine } from "../api/runs";
import { type Task, upsert } from "../api/tasks";
import { runRoute } from "../router";
import { AgentLog, usd } from "./AgentLog";
import { ChildOf } from "./Runs";
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
      .then(({ data }) => data && setLog((prev) => merge(data, prev)))
      .catch(() => undefined);
  }, [runId, stageId]);

  useEffect(() => {
    setVerdicts([]);
    if (!stageId) return;
    api
      .GET("/api/runs/{run_id}/stages/{stage_id}/verdicts", {
        params: { path: { run_id: runId, stage_id: stageId } },
      })
      .then(({ data }) => data && setVerdicts((prev) => byLoop(data, prev)))
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
      if (entry.stage_id === stageId) setLog((prev) => merge(prev, [entry]));
    },
    "verdict.created": (verdict) => {
      if (verdict.stage_id === stageId) setVerdicts((prev) => byLoop(prev, [verdict]));
    },
    "artifact.created": (created) => {
      if (created.stage_id === stageId) setArtifactsSeen((n) => n + 1);
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

  const crumb = (
    <div className="crumb">
      <Link to="/">worlds</Link> /{" "}
      <Link to="/worlds/$worldId/runs" params={{ worldId }}>
        runs
      </Link>
    </div>
  );
  const cost = Object.values(agents).reduce((sum, a) => sum + a.cost_usd, 0);
  if (run === undefined) {
    return <main>{error ? <p role="alert">{error}</p> : <p role="status">Loading...</p>}</main>;
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
  return (
    <main>
      <header className="page">
        <div>
          {crumb}
          <h1>
            {run.name} <span className="faint-title">v{run.workflow_version}</span>
          </h1>
          {run.parent && (
            <p className="sub">
              <ChildOf parent={run.parent} />. It spends that run's budget, and aborting that run
              aborts this one.
            </p>
          )}
          <p className="sub" role="status" data-testid="status-line">
            {statusLine(run)}
            {run.send_backs > 0 && ` Sent back ${run.send_backs} of 2 times.`}
          </p>
          {cost > 0 && (
            <p className="faint" data-testid="run-cost">
              Estimated agent cost so far: {usd(cost)}
              {Object.values(agents).some((a) => a.run_id !== run.id) && ", child runs included"}. An
              estimate from the Agent SDK, not your bill.
            </p>
          )}
        </div>
        {!ended && (
          <div className="row" data-testid="run-actions">
            {run.status === "waiting_gate" && (
              <Link to="/gates" className="button">
                Answer the gate
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
              {s.error && <span className="stage-error">{s.error}</span>}
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

/** Verdicts by loop, each once, oldest first. */
function byLoop(a: Verdict[], b: Verdict[]): Verdict[] {
  const byId = new Map([...a, ...b].map((v) => [v.loop, v]));
  return [...byId.values()].sort((x, y) => x.loop - y.loop);
}

/** Log entries by seq, each once, oldest first. */
function merge(a: LogEntry[], b: LogEntry[]): LogEntry[] {
  const bySeq = new Map([...a, ...b].map((e) => [e.seq, e]));
  return [...bySeq.values()].sort((x, y) => x.seq - y.seq);
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
      {call.error && <p role="alert">{call.error}</p>}
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
