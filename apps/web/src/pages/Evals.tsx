import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { money, took } from "../api/runs";
import type { components } from "../api/schema";

type Report = components["schemas"]["EvalReport"];
type Trial = components["schemas"]["Trial"];

const ARM_LABEL: Record<string, string> = { baseline: "Hooli baseline", swarm: "Swarm" };
const pct = (x: number) => `${Math.round(x * 100)}%`;
const when = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const passed = (t: Trial) => t.status === "done" && (t.failed ?? []).length === 0;

/** The eval reports (baseline against swarm), newest first; the newest in full. */
export function Evals() {
  const [reports, setReports] = useState<Report[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/evals")
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setReports(data);
      })
      .catch(() => setError("Could not load the evals. Is the API running?"));
  }, []);

  const [latest, ...earlier] = reports ?? [];
  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">swarmify</Link> / evals
          </div>
          <h1>Evals: Hooli baseline against the swarm</h1>
          <p className="sub">
            Each task runs as the weekly short in a fresh world, k times one agent alone and k times the swarm. Only
            what the run produced is graded. Cost is the runtime's estimate, not billing.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {reports === null && !error && <p role="status">Loading...</p>}
      {reports && !latest && (
        <div className="empty" data-testid="no-evals">
          <p>
            No eval yet. With the API running, run <code>uv run swarmify-eval --k 3</code>; the report lands here and
            in the Evals world's library.
          </p>
        </div>
      )}
      {latest && (
        <>
          <p className="trend" data-testid="verdict">
            {latest.verdict}
          </p>
          <p className="faint">
            {when(latest.created_at)} · {latest.tasks} tasks · k={latest.k}
          </p>
          <table className="scores" data-testid="arms">
            <thead>
              <tr>
                <th>Arm</th>
                <th>Trials</th>
                <th>Accepted</th>
                <th>pass@{latest.k}</th>
                <th>pass^{latest.k}</th>
                <th>Cost a trial (est.)</th>
                <th>Time to accepted</th>
              </tr>
            </thead>
            <tbody>
              {latest.arms.map((a) => (
                <tr key={a.arm} data-testid="arm">
                  <td>{ARM_LABEL[a.arm] ?? a.arm}</td>
                  <td>{a.trials}</td>
                  <td>
                    {pct(a.acceptance)} ({a.accepted})
                  </td>
                  <td>{pct(a.pass_at_k)}</td>
                  <td>{pct(a.pass_pow_k)}</td>
                  <td>{money(a.cost_usd)}</td>
                  <td>{a.duration_s === null ? "none accepted" : took(a.duration_s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <details>
            <summary>Every trial ({latest.trials.length})</summary>
            <table className="scores" data-testid="trials">
              <thead>
                <tr>
                  <th>Task</th>
                  <th>Arm</th>
                  <th>Trial</th>
                  <th>Result</th>
                  <th>Cost (est.)</th>
                  <th>Duration</th>
                </tr>
              </thead>
              <tbody>
                {latest.trials.map((t) => (
                  <tr key={`${t.task_id}-${t.arm}-${t.index}`} data-testid="trial">
                    <td>{t.task_id}</td>
                    <td>{ARM_LABEL[t.arm] ?? t.arm}</td>
                    <td>
                      {t.run_id ? (
                        <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: t.world_id, runId: t.run_id }}>
                          {t.index + 1}
                        </Link>
                      ) : (
                        t.index + 1
                      )}
                    </td>
                    <td>{passed(t) ? "accepted" : (t.failed ?? []).join("; ") || t.status}</td>
                    <td>{money(t.cost_usd)}</td>
                    <td>{t.duration_s === null ? "none" : took(t.duration_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
      {earlier.length > 0 && (
        <section>
          <h2>Earlier evals</h2>
          <ul data-testid="earlier">
            {earlier.map((r) => (
              <li key={r.id}>
                {when(r.created_at)}: {r.verdict}
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
