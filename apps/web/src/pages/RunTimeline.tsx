import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { type Run, statusLine, useRunUpdates } from "../api/runs";
import { runRoute } from "../router";

export function RunTimeline() {
  const { worldId, runId } = runRoute.useParams();
  const [run, setRun] = useState<Run | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

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

  useRunUpdates(worldId, (update) => {
    if (update.id === runId) setRun(update);
  });

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
        {run.stages.map((s) => (
          <li key={s.id} className={`stage stage-${s.status}`} data-status={s.status}>
            <span className="stage-head">
              {s.index + 1} · {s.status === "waiting" ? "you" : s.status} · {s.type}
            </span>
            <b>{s.name}</b>
            {s.error && <span className="stage-error">{s.error}</span>}
          </li>
        ))}
      </ol>
    </main>
  );
}
