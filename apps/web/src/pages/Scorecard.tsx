import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { money, type Run, STATUS_LABEL, took } from "../api/runs";
import type { components } from "../api/schema";

type Card = components["schemas"]["Scorecard"];
type Score = components["schemas"]["RunScore"];

const RECENT = 3;

/** "2 of 3" first-try stages, or "none judged". */
export function firstTry(s: Score): string {
  return s.judged === 0 ? "none judged" : `${s.first_try} of ${s.judged}`;
}

/** Averages over a set of runs, for the trend line. */
function averages(runs: Score[]) {
  const judged = runs.reduce((n, r) => n + r.judged, 0);
  const first = runs.reduce((n, r) => n + r.first_try, 0);
  const rate = judged === 0 ? null : Math.round((first / judged) * 100);
  const mean = (pick: (r: Score) => number | null) => {
    const xs = runs.flatMap((r) => (pick(r) === null ? [] : [pick(r) as number]));
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  };
  return { rate, sendBacks: mean((r) => r.send_backs), cost: mean((r) => r.cost_usd), duration: mean((r) => r.duration_s) };
}

/** One sentence comparing the last three swarm runs with the ones before them. */
export function trendLine(runs: Score[]): string | null {
  const swarm = runs.filter((r) => !r.baseline);
  if (swarm.length <= RECENT) return null;
  const recent = averages(swarm.slice(-RECENT));
  const before = averages(swarm.slice(0, -RECENT));
  const pct = (n: number | null) => (n === null ? "none judged" : `${n}%`);
  const parts = [
    `first-try acceptance ${pct(recent.rate)} (was ${pct(before.rate)})`,
    `${(recent.sendBacks ?? 0).toFixed(1)} send-backs a run (was ${(before.sendBacks ?? 0).toFixed(1)})`,
    `${money(recent.cost ?? 0)} a run (was ${money(before.cost ?? 0)})`,
  ];
  if (recent.duration !== null && before.duration !== null) {
    parts.push(`${took(recent.duration)} a run (was ${took(before.duration)})`);
  }
  return `Last ${RECENT} runs against the ${swarm.length - RECENT} before: ${parts.join(", ")}.`;
}

/** The editor's Scorecard tab: every run of the workflow, oldest first. */
export function Scorecard({ worldId, workflowId }: { worldId: string; workflowId: string }) {
  const [card, setCard] = useState<Card | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/workflows/{workflow_id}/scorecard", { params: { path: { workflow_id: workflowId } } })
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setCard(data);
      })
      .catch(() => setError("Could not load the scorecard. Is the API running?"));
  }, [workflowId]);

  const runs = card?.runs ?? [];
  const trend = trendLine(runs);
  return (
    <div data-testid="scorecard">
      <p className="sub trend">
        How often Erlich passed a stage first try, how often you sent one back, what Russ counted,
        how long it took, and what the published piece did. Cost is the runtime's estimate, not
        billing.
      </p>
      {error && <p role="alert">{error}</p>}
      {card === null && !error && <p role="status">Loading...</p>}
      {card && runs.length === 0 && (
        <div className="empty" data-testid="no-scores">
          <p>No runs yet. The first row lands here when a run of this workflow starts.</p>
          <Link to="/worlds/$worldId/workflows" params={{ worldId }} className="button">
            Start a run
          </Link>
        </div>
      )}
      {runs.length > 0 && (
        <>
          {trend && (
            <p className="trend" data-testid="trend">
              {trend}
            </p>
          )}
          <div className="scroll-x">
            <table className="scores" data-testid="scores">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Version</th>
                  <th>Status</th>
                  <th className="num">First try</th>
                  <th className="num">Send-backs</th>
                  <th className="num">Cost (est.)</th>
                  <th className="num">Duration</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id} data-testid="score" data-baseline={r.baseline}>
                    <td>
                      <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: r.run_id }}>
                        {new Date(r.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
                      </Link>
                      {r.baseline && <span className="faint"> Hooli baseline</span>}
                    </td>
                    <td>{r.workflow_version === null ? "ad hoc" : `v${r.workflow_version}`}</td>
                    <td>
                      <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status as Run["status"]] ?? r.status}</span>
                    </td>
                    <td className="num">{firstTry(r)}</td>
                    <td className="num">{r.send_backs}</td>
                    <td className="num">{money(r.cost_usd)}</td>
                    <td className="num">{r.duration_s === null ? "running" : took(r.duration_s)}</td>
                    <td>{r.views === null ? "not measured" : `${r.views.toLocaleString()} views`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
