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

/** The eval reports (baseline against swarm), newest first; the newest verdict leads the page. */
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

  const latest = reports?.[0];
  return (
    <main>
      <header className="page">
        <div>
          <h1>Evals</h1>
          <p className="sub" data-testid="verdict">
            {latest ? latest.verdict : "Hooli baseline against the swarm on the same tasks."}
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
      {reports && reports.length > 0 && (
        <>
          <p className="faint">
            Each task runs as the weekly short in a fresh world, k times one agent alone and k times the swarm. Only
            what the run produced is graded, then a blind judge picks the better of each pair without knowing which
            arm made it. Cost is the runtime's estimate, not billing.
          </p>
          <div className="eval-list">{reports.map((r) => <Eval key={r.id} report={r} />)}</div>
        </>
      )}
    </main>
  );
}

/** One report: its verdict, baseline against swarm in one table, then the judge's picks and every trial. */
function Eval({ report }: { report: Report }) {
  return (
    <section className="panel" data-testid="eval" aria-label={`Eval of ${when(report.created_at)}`}>
      <div className="panel-head">
        <h2>{when(report.created_at)}</h2>
        <span className={report.swarm_wins ? "pill positive" : "pill negative"}>
          {report.swarm_wins ? "Swarm wins" : "Swarm does not win"}
        </span>
      </div>
      <p>{report.verdict}</p>
      <div className="scroll-x">
        <table className="scores" data-testid="arms">
          <thead>
            <tr>
              <th>Arm</th>
              <th className="num">Trials</th>
              <th className="num">Accepted</th>
              <th className="num">pass@{report.k}</th>
              <th className="num">pass^{report.k}</th>
              <th className="num">Cost a trial (est.)</th>
              <th className="num">Time to accepted</th>
              <th className="num">Wins</th>
              <th className="num">Losses</th>
              <th className="num">Ties</th>
              <th className="num">Cost a win (est.)</th>
            </tr>
          </thead>
          <tbody>
            {report.arms.map((a) => (
              <tr key={a.arm} data-testid="arm">
                <td>{ARM_LABEL[a.arm] ?? a.arm}</td>
                <td className="num">{a.trials}</td>
                <td className="num">
                  {pct(a.acceptance)} ({a.accepted})
                </td>
                <td className="num">{pct(a.pass_at_k)}</td>
                <td className="num">{pct(a.pass_pow_k)}</td>
                <td className="num">{money(a.cost_usd)}</td>
                <td className="num">{a.duration_s === null ? "none accepted" : took(a.duration_s)}</td>
                <td className="num">{a.wins}</td>
                <td className="num">{a.losses}</td>
                <td className="num">{a.ties}</td>
                <td className="num">{a.cost_per_win == null ? "no win" : money(a.cost_per_win)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <details>
        <summary>The judge's picks ({(report.judgements ?? []).length})</summary>
        <table className="scores" data-testid="judgements">
          <thead>
            <tr>
              <th>Task</th>
              <th>Trial</th>
              <th>Preferred</th>
              <th>Why</th>
            </tr>
          </thead>
          <tbody>
            {(report.judgements ?? []).map((j) => (
              <tr key={`${j.task_id}-${j.index}`} data-testid="judgement">
                <td>{j.task_id}</td>
                <td>{j.index + 1}</td>
                <td>{j.winner ? (ARM_LABEL[j.winner] ?? j.winner) : "tie"}</td>
                <td>{j.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      <details>
        <summary>Every trial ({report.trials.length})</summary>
        <table className="scores" data-testid="trials">
          <thead>
            <tr>
              <th>Task</th>
              <th>Arm</th>
              <th>Trial</th>
              <th>Result</th>
              <th className="num">Cost (est.)</th>
              <th className="num">Duration</th>
            </tr>
          </thead>
          <tbody>
            {report.trials.map((t) => (
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
                <td className="num">{money(t.cost_usd)}</td>
                <td className="num">{t.duration_s === null ? "none" : took(t.duration_s)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      <p className="faint">
        {report.tasks} {report.tasks === 1 ? "task" : "tasks"}, k={report.k}
      </p>
    </section>
  );
}
