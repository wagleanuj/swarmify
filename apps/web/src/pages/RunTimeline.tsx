import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { type Call, type Run, statusLine, useRunUpdates } from "../api/runs";
import { runRoute } from "../router";

export function RunTimeline() {
  const { worldId, runId } = runRoute.useParams();
  const [run, setRun] = useState<Run | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [calls, setCalls] = useState<Record<string, Call | null>>({});
  const [open, setOpen] = useState<string | null>(null);

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

  useRunUpdates(
    worldId,
    (update) => {
      if (update.id === runId) setRun(update);
    },
    (call) => {
      if (call.run_id === runId) setCalls((prev) => ({ ...prev, [call.idempotency_key]: call }));
    },
  );

  function toggle(stageId: string) {
    setOpen((prev) => (prev === stageId ? null : stageId));
    if (calls[stageId]) return;
    api
      .GET("/api/stages/{stage_id}/call", { params: { path: { stage_id: stageId } } })
      .then(({ data }) => setCalls((prev) => ({ ...prev, [stageId]: prev[stageId] ?? data ?? null })))
      .catch(() => undefined);
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
  return (
    <main>
      <header className="page">
        <div>
          {crumb}
          <h1>
            {run.name} <span className="faint-title">v{run.workflow_version}</span>
          </h1>
          <p className="sub" role="status" data-testid="status-line">
            {statusLine(run)}
          </p>
        </div>
      </header>
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
              {s.error && <span className="stage-error">{s.error}</span>}
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
    </main>
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
