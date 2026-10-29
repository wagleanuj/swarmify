import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import { useOpenGates } from "../api/gates";
import { duration, money, type Run, STATUS_LABEL, took } from "../api/runs";
import type { components } from "../api/schema";
import { worldRoute } from "../router";

type World = components["schemas"]["World"];
type Workflow = components["schemas"]["ListedWorkflow"];
type Score = components["schemas"]["RunScore"];

const SHOWN = 5;
// A run holds one of its world's slots from leaving the queue until it ends; a child run
// waits on its own queue and spends its parent's budget, so figures count top runs only.
const HOLDS_SLOT = new Set<Run["status"]>(["running", "waiting_gate", "paused"]);

/** The strip of figures: runs and spend today, slots in use, first-try acceptance. */
export function figures(runs: Run[], scores: Score[], now = new Date()) {
  const top = runs.filter((r) => !r.parent_run_id);
  const today = top.filter((r) => new Date(r.created_at).toDateString() === now.toDateString());
  return {
    today: today.length,
    spent: today.reduce((n, r) => n + r.spent_usd, 0),
    inUse: top.filter((r) => HOLDS_SLOT.has(r.status)).length,
    firstTry: scores.reduce((n, s) => n + s.first_try, 0),
    judged: scores.reduce((n, s) => n + s.judged, 0),
  };
}

export function WorldOverview() {
  const { worldId } = worldRoute.useParams();
  const router = useRouter();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [scores, setScores] = useState<Score[]>([]);
  const gates = (useOpenGates().gates ?? []).filter((g) => g.world_id === worldId);
  const [starting, setStarting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [goal, setGoal] = useState("");
  const [goalBudget, setGoalBudget] = useState("");
  const [runBudget, setRunBudget] = useState("");
  const [agents, setAgents] = useState("");

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data, response }) => {
        if (response.status === 404) return setWorld(null);
        if (!data) throw new Error("load failed");
        setWorld(data);
      })
      .catch(() => setError("Could not load this world. Is the API running?"));
  }, [worldId]);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorkflows(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/runs", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setRuns(data))
      .catch(() => undefined);
  }, [worldId]);

  // ponytail: one scorecard per workflow, read when the list loads, so a run judged while the page
  // is open shows on the next visit. Add a world scorecard endpoint when worlds hold many workflows.
  useEffect(() => {
    void Promise.all(
      workflows.map((w) =>
        api
          .GET("/api/workflows/{workflow_id}/scorecard", { params: { path: { workflow_id: w.id } } })
          .then(({ data }) => data?.runs ?? [])
          .catch(() => []),
      ),
    ).then((cards) => setScores(cards.flat()));
  }, [workflows]);

  // Skins render events, they never poll tables (ADR 0002). Native EventSource, no library.
  // Live only: the runs come from the GET above. The gates follow the shared all-worlds stream.
  useEffect(() => {
    const source = new EventSource(`/api/worlds/${worldId}/events?since=now`);
    source.addEventListener("run.updated", (e) => {
      const run = (JSON.parse((e as MessageEvent<string>).data) as { payload?: Run }).payload;
      if (!run) return;
      setRuns((prev) => (prev.some((r) => r.id === run.id) ? prev.map((r) => (r.id === run.id ? run : r)) : [run, ...prev]));
    });
    source.addEventListener("workflow.saved", () => {
      void api
        .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
        .then(({ data }) => data && setWorkflows(data));
    });
    return () => source.close();
  }, [worldId]);

  async function start(workflowId: string, baseline = false) {
    setStarting(workflowId);
    setError(null);
    const { data, error, response } = await api.POST("/api/workflows/{workflow_id}/runs", {
      params: { path: { workflow_id: workflowId } },
      // Blank fields are left out: the workflow's budget and agents, else the world's, apply.
      body: {
        baseline,
        ...(runBudget && { budget_usd: Number(runBudget) }),
        ...(agents && !baseline && { agents: Number(agents) }),
      },
    });
    setStarting(null);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not started: ${detail}.` : `Could not start the run (${response.status}).`);
      return;
    }
    void router.navigate({
      to: "/worlds/$worldId/runs/$runId",
      params: { worldId, runId: data.id },
    });
  }

  async function startAdHoc(e: FormEvent) {
    e.preventDefault();
    setStarting("ad-hoc");
    setError(null);
    const { data, error, response } = await api.POST("/api/worlds/{world_id}/runs", {
      params: { path: { world_id: worldId } },
      body: { goal, done_when: "", baseline: false, ...(goalBudget && { budget_usd: Number(goalBudget) }) },
    });
    setStarting(null);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not started: ${detail}.` : `Could not start the run (${response.status}).`);
      return;
    }
    void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId: data.id } });
  }

  if (world === undefined) {
    return (
      <main>
        {error ? <p role="alert">{error}</p> : <p role="status">Loading...</p>}
      </main>
    );
  }
  if (world === null) {
    return (
      <main>
        <h1>No such world</h1>
        <p className="sub">
          It may have been deleted. <Link to="/">Back to your worlds.</Link>
        </p>
      </main>
    );
  }

  const f = figures(runs, scores);
  return (
    <main className="overview">
      <header className="page">
        <div>
          <h1>{world.name}</h1>
          <p className="sub">{world.purpose || "No purpose written yet."}</p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <section className="kpis" aria-label="Figures">
        <div className="kpi">
          <div className="kpi-label">Runs today</div>
          <div className="kpi-value num">{f.today}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Spent today</div>
          <div className="kpi-value num">{money(f.spent)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Run slots in use</div>
          <div className="kpi-value num">
            {f.inUse} <small>of {world.run_cap}</small>
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Accepted first try</div>
          <div className="kpi-value num">
            {f.firstTry} <small>of {f.judged} judged</small>
          </div>
        </div>
      </section>
      <div className="cols-main">
        <div className="stack">
          <section className="panel" aria-label="Runs">
            <div className="panel-head">
              <h2>Runs</h2>
              {runs.length > 0 && (
                <Link to="/worlds/$worldId/runs" params={{ worldId }} className="card-link">
                  View all {runs.length}
                </Link>
              )}
            </div>
            {runs.length === 0 ? (
              <p className="faint">None yet. Start one from a workflow or a goal.</p>
            ) : (
              <table className="scores" data-testid="runs">
                <thead>
                  <tr>
                    <th>Run</th>
                    <th>Status</th>
                    <th className="num">Started</th>
                    <th className="num">Time</th>
                    <th className="num">Spent</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.slice(0, SHOWN).map((r) => {
                    const seconds = duration(r);
                    return (
                      <tr key={r.id}>
                        <td>
                          <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: r.id }}>
                            {r.name}
                          </Link>
                          {r.workflow_version !== null && <span className="faint"> v{r.workflow_version}</span>}
                        </td>
                        <td>
                          <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status]}</span>
                        </td>
                        <td className="num">
                          {new Date(r.created_at).toLocaleTimeString([], { timeStyle: "short" })}
                        </td>
                        <td className="num">{seconds === null ? "" : took(seconds)}</td>
                        <td className="num">{money(r.spent_usd)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
          <form className="panel" onSubmit={(e) => void startAdHoc(e)} aria-label="Start from a goal">
            <div className="panel-head">
              <h2>Start from a goal</h2>
              <span className="faint">One swarm stage, every connector this world allows</span>
            </div>
            <div className="goal">
              <input
                required
                aria-label="Goal"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                placeholder="Find five hooks for next week's short."
              />
              <input
                type="number"
                min="0.01"
                step="0.01"
                aria-label="Budget in USD"
                value={goalBudget}
                onChange={(e) => setGoalBudget(e.target.value)}
                placeholder="Budget"
              />
              <button type="submit" disabled={starting !== null}>
                {starting === "ad-hoc" ? "Starting..." : "Run goal"}
              </button>
            </div>
          </form>
        </div>
        <div className="stack">
          <section className="panel" aria-label="Needs you" data-testid="needs-you">
            <div className="panel-head">
              <h2>Needs you</h2>
              {gates.length > 0 && <span className="pill warning">{gates.length} waiting</span>}
            </div>
            {gates.length === 0 ? (
              <p className="faint">Nothing waits for you in this world.</p>
            ) : (
              <ul className="rows">
                {gates.map((g) => (
                  <li key={g.stage_id} className="row">
                    <span className="dot warn" />
                    <span className="grow">
                      {g.stage_name}
                      <span className="faint">
                        {" "}
                        {g.run_name} · waiting since{" "}
                        {new Date(g.opened_at).toLocaleTimeString([], { timeStyle: "short" })}
                      </span>
                    </span>
                    <Link
                      to="/gates"
                      search={{ world: worldId, gate: g.stage_id }}
                      className="button"
                    >
                      Answer
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
          <section className="panel workflows" aria-label="Workflows" id="workflows">
            <div className="panel-head">
              <h2>Workflows</h2>
              <Link to="/worlds/$worldId/workflows/new" params={{ worldId }} className="card-link">
                New workflow
              </Link>
            </div>
            {workflows.length === 0 ? (
              <p className="faint" data-testid="no-workflows">
                A workflow is the recipe a run follows: research, pick, script, render, review,
                publish. Write the first one in YAML and the editor checks every rule as you type.
              </p>
            ) : (
              <>
                <ul className="rows" data-testid="workflow-list">
                  {workflows.map((w) => (
                    <li key={w.id} className="row">
                      <span className="grow">
                        <Link to="/worlds/$worldId/workflows/$workflowId" params={{ worldId, workflowId: w.id }}>
                          {w.name}
                        </Link>
                        <span className="faint"> v{w.current_version}</span>
                        {w.violations?.slice(0, 1).map((v) => (
                          <span key={v.rule} className="chip warn" data-testid={`fails-check-${w.id}`}>
                            Fails today's check: {v.message}. Save a new version to run it.
                          </span>
                        ))}
                      </span>
                      <span className="actions">
                        <Link
                          to="/worlds/$worldId/workflows/$workflowId/scorecard"
                          params={{ worldId, workflowId: w.id }}
                          className="card-link"
                          aria-label={`Scorecard of ${w.name}`}
                        >
                          Scorecard
                        </Link>
                        <button
                          type="button"
                          onClick={() => start(w.id, true)}
                          disabled={starting !== null}
                          aria-label={`Start a Hooli baseline of ${w.name}`}
                          title="One agent and no graph, so the swarm's runs have something to beat"
                        >
                          Baseline
                        </button>
                        <button
                          type="button"
                          onClick={() => start(w.id)}
                          disabled={starting !== null}
                          aria-label={`Start a run of ${w.name}`}
                        >
                          {starting === w.id ? "Starting..." : "Start run"}
                        </button>
                      </span>
                    </li>
                  ))}
                </ul>
                <fieldset aria-label="Overrides" className="overrides">
                  <legend className="faint">For the next run you start above; blank keeps the workflow's</legend>
                  <label>
                    Run budget in USD
                    <input type="number" min="0.01" step="0.01" value={runBudget} onChange={(e) => setRunBudget(e.target.value)} />
                  </label>
                  <label>
                    Workers per swarm stage
                    <input type="number" min="1" max="8" step="1" value={agents} onChange={(e) => setAgents(e.target.value)} />
                  </label>
                </fieldset>
              </>
            )}
          </section>
        </div>
      </div>
    </main>
  );
}
