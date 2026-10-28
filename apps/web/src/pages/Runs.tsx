import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { duration, money, progress, type Run, type RunLink, STATUS_LABEL, took, useRunUpdates } from "../api/runs";
import type { components } from "../api/schema";
import { runsRoute } from "../router";

type World = components["schemas"]["World"];
type Workflow = components["schemas"]["Workflow"];

const AD_HOC = "ad-hoc";

/** "took 2 min" once a run ended, "running 2 min" while it goes; nothing before a stage starts. */
function elapsed(r: Run): string | null {
  const seconds = duration(r);
  if (seconds === null) return null;
  return ["done", "failed", "aborted"].includes(r.status) ? `took ${took(seconds)}` : `${took(seconds)} so far`;
}

export function Runs() {
  const { worldId } = runsRoute.useParams();
  const [world, setWorld] = useState<World | null>(null);
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflow, setWorkflow] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorld(data ?? null))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorkflows(data ?? []))
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

  // ponytail: filtered here, not by the API, so live updates land in the same filter. Add query
  // params to GET /runs when a world's run list grows past what one response carries.
  const shown = (runs ?? []).filter(
    (r) =>
      (!workflow || (workflow === AD_HOC ? r.workflow_id === null : r.workflow_id === workflow)) &&
      (!status || r.status === status),
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
        <fieldset aria-label="Filters">
          <label>
            Workflow
            <select value={workflow} onChange={(e) => setWorkflow(e.target.value)}>
              <option value="">All workflows</option>
              {workflows.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
              <option value={AD_HOC}>Ad hoc</option>
            </select>
          </label>
          <label>
            Status
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Any status</option>
              {Object.entries(STATUS_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </fieldset>
      )}
      {runs && runs.length > 0 && shown.length === 0 && (
        <p className="faint" data-testid="no-match">
          No runs match these filters.
        </p>
      )}
      {shown.length > 0 && (
        <ul className="run-list" data-testid="run-list">
          {shown.map((r) => (
            <li key={r.id}>
              <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: r.id }}>
                {r.name}
              </Link>
              <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status]}</span>
              {r.parent && <RunRef label="child of" run={r.parent} testId="child-of" />}
              {r.forked_from && <RunRef label="fork of" run={r.forked_from} testId="fork-of" />}
              {r.baseline && <span className="faint">Hooli baseline</span>}
              <span className="faint">{progress(r)}</span>
              {r.spent_usd > 0 && <span className="faint">{money(r.spent_usd)}</span>}
              {elapsed(r) && <span className="faint">{elapsed(r)}</span>}
              <span className="faint">{version(r)}</span>
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

/** "v3", or "ad hoc" for a run started from a typed goal with no workflow. */
export const version = (r: Run) => (r.workflow_version === null ? "ad hoc" : `v${r.workflow_version}`);

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
