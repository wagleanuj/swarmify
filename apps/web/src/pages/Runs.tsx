import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { progress, type Run, type RunLink, STATUS_LABEL, useRunUpdates } from "../api/runs";
import type { components } from "../api/schema";
import { runsRoute } from "../router";

type World = components["schemas"]["World"];

export function Runs() {
  const { worldId } = runsRoute.useParams();
  const [world, setWorld] = useState<World | null>(null);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorld(data ?? null))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/runs", { params: { path: { world_id: worldId } } })
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setRuns(data);
      })
      .catch(() => setError("Could not load runs. Is the API running?"));
  }, [worldId]);

  useRunUpdates(worldId, (run) =>
    setRuns((prev) => {
      const list = prev ?? [];
      return list.some((r) => r.id === run.id)
        ? list.map((r) => (r.id === run.id ? run : r))
        : [run, ...list];
    }),
  );

  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> /{" "}
            <Link to="/worlds/$worldId" params={{ worldId }}>
              {world?.name.toLowerCase() ?? worldId}
            </Link>{" "}
            / runs
          </div>
          <h1>Runs</h1>
          <p className="sub">Every run of this world, newest first. Statuses update live.</p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {runs === null && !error && <p role="status">Loading...</p>}
      {runs?.length === 0 && (
        <div className="empty" data-testid="no-runs">
          <p>
            A run follows one workflow from its first stage to its last, and you watch each stage
            land here as it happens.
          </p>
          <Link to="/worlds/$worldId" params={{ worldId }} className="button">
            Start one from a workflow
          </Link>
        </div>
      )}
      {runs && runs.length > 0 && (
        <ul className="run-list" data-testid="run-list">
          {runs.map((r) => (
            <li key={r.id}>
              <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: r.id }}>
                {r.name}
              </Link>
              <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status]}</span>
              {r.parent && <RunRef label="child of" run={r.parent} testId="child-of" />}
              {r.forked_from && <RunRef label="fork of" run={r.forked_from} testId="fork-of" />}
              <span className="faint">{progress(r)}</span>
              <span className="faint">v{r.workflow_version}</span>
              <time className="faint" dateTime={r.created_at}>
                {new Date(r.created_at).toLocaleString()}
              </time>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

/** "<label> <run>", linking to a related run. */
export function RunRef({ label, run, testId }: { label: string; run: RunLink; testId?: string }) {
  return (
    <span className="faint" data-testid={testId}>
      {label}{" "}
      <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: run.world_id, runId: run.id }}>
        {run.name}
      </Link>
    </span>
  );
}
