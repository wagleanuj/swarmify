import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { clock, duration, money, type Run, type RunLink, STATUS_LABEL, useRunUpdates } from "../api/runs";
import type { components } from "../api/schema";
import { runsRoute } from "../router";
import { stageSeconds } from "./StagePanel";
import { TimeStrip } from "./TimeStrip";

type Workflow = components["schemas"]["Workflow"];

const AD_HOC = "ad-hoc";

/** The status tabs: the value kept in the address, its name, and which runs it holds. */
type Tab = [string, string, (r: Run) => boolean];
const ALL: Tab = ["", "All", () => true];
const TABS: Tab[] = [
  ALL,
  ["waiting", "Waiting for you", (r) => r.status === "waiting_gate"],
  ["running", "Running", (r) => ["running", "queued", "paused"].includes(r.status)],
  ["done", "Done", (r) => r.status === "done"],
  ["failed", "Failed or aborted", (r) => ["failed", "aborted"].includes(r.status)],
];

/** Seconds per step name across the runs, longest first, and whether that step is a gate. */
function timeWent(runs: Run[]): [string, { secs: number; gate: boolean }][] {
  const by = new Map<string, { secs: number; gate: boolean }>();
  for (const r of runs)
    for (const s of r.stages) {
      const secs = stageSeconds(s);
      if (secs === null) continue;
      const row = by.get(s.name) ?? { secs: 0, gate: s.type === "gate" };
      row.secs += secs;
      by.set(s.name, row);
    }
  return [...by].sort((a, b) => b[1].secs - a[1].secs);
}

const spentOf = (runs: Run[]) => runs.reduce((sum, r) => sum + r.spent_usd, 0);
const shortId = (id: string) => (id.length > 10 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id);
export const when = (iso: string) =>
  new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

export function Runs() {
  const { worldId } = runsRoute.useParams();
  const search = runsRoute.useSearch();
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [workflow, setWorkflow] = useState("");

  useEffect(() => {
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
  const all = runs ?? [];
  const ofWorkflow = all.filter(
    (r) => !workflow || (workflow === AD_HOC ? r.workflow_id === null : r.workflow_id === workflow),
  );
  const tab = TABS.find(([v]) => v === (search.status ?? "")) ?? ALL;
  const shown = ofWorkflow.filter(tab[2]);
  const steps = timeWent(all);
  const top = steps[0]?.[1].secs || 1;
  const names = new Map(workflows.map((w) => [w.id, w.name]));
  const versions = new Map<string, Run[]>();
  for (const r of all) {
    const key = r.workflow_id === null ? "Ad hoc" : `${names.get(r.workflow_id) ?? r.name} ${version(r)}`;
    versions.set(key, [...(versions.get(key) ?? []), r]);
  }

  return (
    <main>
      <header className="page">
        <div>
          <h1>Runs</h1>
          <p className="sub">
            {runs && runs.length > 0
              ? `${runs.length} ${runs.length === 1 ? "run" : "runs"}, ${money(spentOf(runs))} spent. ${
                  steps[0] ? `Most of the time went to ${steps[0][0]}: ${clock(steps[0][1].secs)} across these runs.` : ""
                }`
              : "Every run of this world, newest first. Statuses update live."}
          </p>
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
        <>
          <nav className="tabs chip-row" aria-label="Status">
            {TABS.map(([v, label, holds]) => (
              <Link
                key={v}
                to="/worlds/$worldId/runs"
                params={{ worldId }}
                search={v ? { status: v } : {}}
                activeOptions={{ exact: true, includeSearch: true }}
                className="tab"
              >
                {label} <span className="num">{ofWorkflow.filter(holds).length}</span>
              </Link>
            ))}
          </nav>
          <form className="filters" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
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
          </form>
          <section className="panel runs-panel">
            {shown.length === 0 ? (
              <p className="faint" data-testid="no-match">
                No runs {tab[0] ? tab[1].toLowerCase() : "of this workflow"} in this world.
              </p>
            ) : (
              <table className="runs-table" data-testid="run-list">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Status</th>
                    <th className="run-strip">Steps</th>
                    <th className="num">Started</th>
                    <th className="num">Time</th>
                    <th className="num">Spent</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((r) => {
                    const secs = duration(r);
                    return (
                      <tr key={r.id}>
                        <td>
                          <span>
                            <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: r.id }}>
                              {r.name}
                            </Link>{" "}
                            <span className="faint">{version(r)}</span>
                          </span>
                          <span className="faint">
                            <span className="mono" title={r.id}>{shortId(r.id)}</span>
                            {r.parent && <RunRef label=" · child of" run={r.parent} testId="child-of" />}
                            {r.forked_from && <RunRef label=" · fork of" run={r.forked_from} testId="fork-of" />}
                            {r.baseline && " · Hooli baseline"}
                          </span>
                        </td>
                        <td>
                          <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status]}</span>
                        </td>
                        <td className="run-strip">
                          <TimeStrip stages={r.stages} labels={false} />
                        </td>
                        <td className="num">
                          <time dateTime={r.created_at}>{when(r.created_at)}</time>
                        </td>
                        <td className="num">{secs === null ? "" : clock(secs)}</td>
                        <td className="num">{money(r.spent_usd)}</td>
                      </tr>
                    );
                  })}
                </tbody>
                <tfoot>
                  <tr>
                    <td colSpan={5} className="faint">
                      {shown.length} {shown.length === 1 ? "run" : "runs"}
                    </td>
                    <td className="num">{money(spentOf(shown))}</td>
                  </tr>
                </tfoot>
              </table>
            )}
          </section>
          <div className="mem-cols">
            <section className="panel" aria-labelledby="time-went">
              <div className="panel-head">
                <h2 id="time-went">Where the time went</h2>
                <span className="faint">All {all.length} runs</span>
              </div>
              <ol className="mem-rows" data-testid="time-went">
                {steps.slice(0, 4).map(([name, { secs, gate }]) => (
                  <li key={name} className="bar-row">
                    <span className="bar-label">
                      <span>
                        {name} <span className="faint">{gate ? "waiting for you" : "agents"}</span>
                      </span>
                      <span className="thin-bar" style={{ width: `${(secs / top) * 100}%` }} />
                    </span>
                    <span className="num">{clock(secs)}</span>
                  </li>
                ))}
              </ol>
              {steps.length === 0 && <p className="faint">No step has started yet.</p>}
            </section>
            <section className="panel" aria-labelledby="by-version">
              <div className="panel-head">
                <h2 id="by-version">Spend by workflow version</h2>
                <span className="faint">All {all.length} runs</span>
              </div>
              <table className="runs-table versions-table" data-testid="spend-by-version">
                <thead>
                  <tr>
                    <th>Version</th>
                    <th className="num">Runs</th>
                    <th className="num">Spent</th>
                    <th className="num">A run</th>
                  </tr>
                </thead>
                <tbody>
                  {[...versions].map(([key, rs]) => (
                    <tr key={key}>
                      <td>{key}</td>
                      <td className="num">{rs.length}</td>
                      <td className="num">{money(spentOf(rs))}</td>
                      <td className="num">{money(spentOf(rs) / rs.length)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <td className="faint">All</td>
                    <td className="num">{all.length}</td>
                    <td className="num">{money(spentOf(all))}</td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </section>
          </div>
        </>
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
