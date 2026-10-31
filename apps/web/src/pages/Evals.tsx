import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { money, took } from "../api/runs";
import type { components } from "../api/schema";
import { baseline, cap } from "../skin";

type Report = components["schemas"]["EvalReport"];
type Trial = components["schemas"]["Trial"];
type Arm = components["schemas"]["ArmSummary"];

const armLabel = (arm: string) => (arm === "baseline" ? cap(baseline()) : arm === "swarm" ? "Swarm" : arm);
const pct = (x: number) => `${Math.round(x * 100)}%`;
const when = (iso: string) => new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
const passed = (t: Trial) => t.status === "done" && (t.failed ?? []).length === 0;
/** The task kinds the swarm evidence eval decides per (ADR 0020), as the API's KINDS. */
const KINDS = ["research briefs", "long-form writing", "typed lists"];
const verdictPill = (r: Report) => (
  <span className={r.swarm_wins ? "pill positive" : "pill negative"}>{r.swarm_wins ? "Swarm wins" : "Swarm does not win"}</span>
);

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
            {latest ? latest.verdict : `${cap(baseline())} against the swarm on the same tasks.`}
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {reports === null && !error && <p role="status">Loading...</p>}
      {reports && !latest && (
        <div className="empty" data-testid="no-evals">
          <p>
            No eval yet. With the API running, run <code>uv run swarmify-eval --kind research-briefs</code> (or{" "}
            <code>long-form-writing</code>, <code>typed-lists</code>); the report lands here and in the Evals world's
            library.
          </p>
        </div>
      )}
      {reports && reports.length > 0 && (
        <>
          <p className="faint">
            Each task runs the starter workflow it names in a fresh world, k times with one agent a stage and k times with a swarm. Only
            what the run produced is graded, then a blind judge picks the better of each pair without knowing which
            arm made it. Cost is the runtime's estimate, not billing.
          </p>
          <Kinds reports={reports} />
          <div className="eval-list">{reports.map((r) => <Eval key={r.id} report={r} />)}</div>
        </>
      )}
    </main>
  );
}

/** Each task kind's verdict, from its newest report, against the bar fixed in advance. */
function Kinds({ reports }: { reports: Report[] }) {
  return (
    <section className="panel" aria-label="Verdict by task kind">
      <div className="panel-head">
        <h2>By task kind</h2>
      </div>
      <div className="scroll-x">
        <table className="scores" data-testid="kinds">
          <thead>
            <tr>
              <th>Kind</th>
              <th>Verdict</th>
              <th className="num">Swarm preferred</th>
              <th className="num">p</th>
              <th className="num">Cost a win against {baseline()}</th>
              <th>Ran</th>
            </tr>
          </thead>
          <tbody>
            {KINDS.map((kind) => {
              const r = reports.find((x) => x.kind === kind);
              if (!r) {
                return (
                  <tr key={kind} data-testid="kind">
                    <td>{cap(kind)}</td>
                    <td colSpan={5} className="faint">
                      Not run yet
                    </td>
                  </tr>
                );
              }
              const [base, swarm] = r.arms as [Arm, Arm]; // baseline then swarm, always both
              const decided = swarm.wins + swarm.losses;
              const ratio = swarm.cost_per_win != null && base.cost_per_win ? swarm.cost_per_win / base.cost_per_win : null;
              return (
                <tr key={kind} data-testid="kind">
                  <td>{cap(kind)}</td>
                  <td>{verdictPill(r)}</td>
                  <td className="num">
                    {swarm.wins} of {decided}
                    {decided > 0 && ` (${pct(swarm.wins / decided)})`}
                  </td>
                  <td className="num">{r.p_value == null ? "none" : r.p_value.toFixed(2)}</td>
                  <td className="num">{ratio === null ? "no comparison" : `${ratio.toFixed(1)}x`}</td>
                  <td>{when(r.created_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="faint">
        A kind is a swarm win only when the judge prefers the swarm in more than 60% of the decided pairs, that share is
        significant (one-sided sign test, p under 0.05), and the swarm costs at most 3 times as much a win. Each kind is 8
        tasks, 2 trials an arm, 5 USD a trial.
      </p>
    </section>
  );
}

/** One report: its verdict, baseline against swarm in one table, then the judge's picks and every trial. */
function Eval({ report }: { report: Report }) {
  return (
    <section className="panel" data-testid="eval" aria-label={`Eval of ${when(report.created_at)}`}>
      <div className="panel-head">
        <h2>{report.kind ? `${cap(report.kind)}, ${when(report.created_at)}` : when(report.created_at)}</h2>
        {verdictPill(report)}
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
                <td>{armLabel(a.arm)}</td>
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
                <td>{j.winner ? armLabel(j.winner) : "tie"}</td>
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
                <td>{armLabel(t.arm)}</td>
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
