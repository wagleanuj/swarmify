import { Link, useParams, useRouter } from "@tanstack/react-router";
import { type ReactNode, useEffect, useState } from "react";

import { api } from "../api/client";
import { money, type Run, STATUS_LABEL } from "../api/runs";
import type { components } from "../api/schema";
import { usePhone } from "../usePhone";
import { when } from "./Runs";

type Workflow = components["schemas"]["ListedWorkflow"];

/** The world's workflows: what each is at, how often it ran, what a run costs, and a start. */
export function Workflows() {
  const { worldId } = useParams({ from: "/worlds/$worldId/workflows" });
  const router = useRouter();
  const [workflows, setWorkflows] = useState<Workflow[] | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [starting, setStarting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const phone = usePhone();

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
      .then(({ data }) => {
        if (!data) throw new Error("load failed");
        setWorkflows(data);
      })
      .catch(() => setError("Could not load the workflows. Is the API running?"));
    api
      .GET("/api/worlds/{world_id}/runs", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setRuns(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  async function start(workflowId: string) {
    setStarting(workflowId);
    setError(null);
    const { data, error, response } = await api.POST("/api/workflows/{workflow_id}/runs", {
      params: { path: { workflow_id: workflowId } },
      body: { baseline: false },
    });
    setStarting(null);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not started: ${detail}.` : `Could not start the run (${response.status}).`);
      return;
    }
    void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId: data.id } });
  }

  return (
    <main>
      <header className="page">
        <div>
          <h1>Workflows</h1>
          <p className="sub">
            {workflows ? `${workflows.length} in this world. ` : ""}
            {phone ? "A run keeps the version it started on." : "Each save is a new version; a run keeps the version it started on."}
          </p>
        </div>
        {!phone && (
          <Link to="/worlds/$worldId/workflows/new" params={{ worldId }} className="button primary">
            New workflow
          </Link>
        )}
      </header>
      {error && <p role="alert">{error}</p>}
      {workflows === null && !error && <p role="status">Loading...</p>}
      {workflows?.length === 0 && (
        <div className="empty">
          <p>No workflows yet. A workflow is a list of steps you can run again and again.</p>
        </div>
      )}
      {phone && workflows && (
        <>
          <ul className="wf-cards">
            {workflows.map((w) => (
              <WorkflowCard key={w.id} worldId={worldId} workflow={w} runs={runs.filter((r) => r.workflow_id === w.id)}>
                <button
                  type="button"
                  className="primary"
                  onClick={() => start(w.id)}
                  disabled={starting !== null}
                  aria-label={`Start a run of ${w.name}`}
                >
                  {starting === w.id ? "Starting..." : "Start run"}
                </button>
              </WorkflowCard>
            ))}
          </ul>
          <Link to="/worlds/$worldId/workflows/new" params={{ worldId }} className="button wf-new">
            New workflow
          </Link>
        </>
      )}
      {!phone && workflows && workflows.length > 0 && (
        <div className="scroll-x">
          <table className="scores workflows" data-testid="workflows">
            <thead>
              <tr>
                <th>Workflow</th>
                <th className="num">Version</th>
                <th className="num">Runs</th>
                <th className="num">Avg cost</th>
                <th>
                  <span className="visually-hidden">Action</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {workflows.map((w) => {
                const mine = runs.filter((r) => r.workflow_id === w.id);
                const cost = mine.reduce((sum, r) => sum + r.spent_usd, 0);
                return (
                  <tr key={w.id} data-testid="workflow">
                    <td>
                      <Link to="/worlds/$worldId/workflows/$workflowId" params={{ worldId, workflowId: w.id }}>
                        {w.name}
                      </Link>
                      {w.violations?.slice(0, 1).map((v) => (
                        <span key={v.rule} className="faint">
                          {" "}
                          Fails today's check: {v.message}.
                        </span>
                      ))}
                    </td>
                    <td className="num">v{w.current_version}</td>
                    <td className="num">{mine.length}</td>
                    <td className="num">{mine.length ? money(cost / mine.length) : "-"}</td>
                    <td className="action">
                      <button
                        type="button"
                        onClick={() => start(w.id)}
                        disabled={starting !== null}
                        aria-label={`Start a run of ${w.name}`}
                      >
                        {starting === w.id ? "Starting..." : "Start run"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}

const dollars = (n: number) => (Number.isInteger(n) ? `$${n}` : money(n));

/** A workflow on a phone: the full name, its numbers on one line with the budget they are measured
 * against (the latest run's), the last run with its state, then Start run and Edit. */
function WorkflowCard({ worldId, workflow: w, runs, children }: { worldId: string; workflow: Workflow; runs: Run[]; children: ReactNode }) {
  const last = runs.reduce<Run | null>((a, r) => (a && a.created_at > r.created_at ? a : r), null);
  const cost = runs.reduce((sum, r) => sum + r.spent_usd, 0) / (runs.length || 1);
  const budget = last?.budget_usd;
  return (
    <li className="card wf-card" data-testid="workflow-card">
      <h2>{w.name}</h2>
      {w.violations?.slice(0, 1).map((v) => (
        <p key={v.rule} className="faint">
          Fails today's check: {v.message}.
        </p>
      ))}
      <p className="faint num">
        v{w.current_version} ·{" "}
        {last
          ? `${runs.length} ${runs.length === 1 ? "run" : "runs"} · about ${money(cost)} a run${budget ? ` of a ${dollars(budget)} budget` : ""}`
          : "no runs yet"}
      </p>
      {last && (
        <p className="faint">
          Last run {when(last.created_at)} ·{" "}
          {last.status === "waiting_gate" ? <span className="warn">waiting for you</span> : STATUS_LABEL[last.status]}
        </p>
      )}
      <div className="wf-actions">
        {children}
        <Link
          to="/worlds/$worldId/workflows/$workflowId"
          params={{ worldId, workflowId: w.id }}
          className="button"
          aria-label={`Edit ${w.name}`}
        >
          Edit
        </Link>
      </div>
    </li>
  );
}
