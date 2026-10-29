import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { Gate, Run } from "../api/runs";
import type { components } from "../api/schema";
import { worldRoute } from "../router";

type World = components["schemas"]["World"];
type Workflow = components["schemas"]["ListedWorkflow"];
type Activity = { seq: number; type: string; ts: string };

/** "run.updated" as "Run updated". */
const inWords = (type: string) => type.replace(".", " ").replace(/^./, (c) => c.toUpperCase());

/** The stream as rows: a run of the same event type collapses into its last one with a count. */
export function collapse(activity: Activity[]): (Activity & { times: number })[] {
  const rows: (Activity & { times: number })[] = [];
  for (const a of activity) {
    const last = rows.at(-1);
    if (last && last.type === a.type) rows[rows.length - 1] = { ...a, times: last.times + 1 };
    else rows.push({ ...a, times: 1 });
  }
  return rows;
}

export function WorldOverview() {
  const { worldId } = worldRoute.useParams();
  const router = useRouter();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [runIds, setRunIds] = useState<Set<string>>(new Set());
  const [gates, setGates] = useState<Gate[]>([]);
  const [starting, setStarting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [goal, setGoal] = useState("");
  const [doneWhen, setDoneWhen] = useState("");
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
      .then(({ data }) => data && setRunIds(new Set(data.map((r) => r.id))))
      .catch(() => undefined);
  }, [worldId]);

  const loadGates = () =>
    api
      .GET("/api/gates")
      .then(({ data }) => data && setGates(data.filter((g) => g.world_id === worldId)))
      .catch(() => undefined);
  useEffect(() => {
    void loadGates();
  }, [worldId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Skins render events, they never poll tables (ADR 0002). Native EventSource, no library.
  useEffect(() => {
    // The activity feed wants history, so this stream replays it. One gates refetch per burst,
    // not per event, or opening a busy world fired one GET /api/gates per past run.updated.
    const source = new EventSource(`/api/worlds/${worldId}/events`);
    let gatesTimer: number | undefined;
    const onEvent = (e: Event) => {
      const data = JSON.parse((e as MessageEvent<string>).data) as Activity & { payload?: Run };
      const run = data.payload;
      if (data.type === "run.updated" && run) {
        setRunIds((prev) => new Set(prev).add(run.id));
        clearTimeout(gatesTimer);
        gatesTimer = window.setTimeout(() => void loadGates(), 100);
      }
      setActivity((prev) => (prev.some((a) => a.seq === data.seq) ? prev : [...prev, data]));
      if (data.type === "workflow.saved") {
        void api
          .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
          .then(({ data }) => data && setWorkflows(data));
      }
    };
    // ponytail: EventSource only delivers named events to matching listeners, so each new event
    // type must be added here. Switch to one generic listener when the API emits unnamed events.
    const types = ["world.created", "world.deleted", "workflow.saved", "run.updated", "gate.opened", "run.ended"];
    for (const type of types) {
      source.addEventListener(type, onEvent);
    }
    return () => {
      clearTimeout(gatesTimer);
      source.close();
    };
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
      body: { goal, done_when: doneWhen, baseline: false, ...(goalBudget && { budget_usd: Number(goalBudget) }) },
    });
    setStarting(null);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not started: ${detail}.` : `Could not start the run (${response.status}).`);
      return;
    }
    void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId: data.id } });
  }

  async function remove() {
    if (!world) return;
    const ok = window.confirm(
      `Delete ${world.name}? Its folder, runs and memory go with it. This cannot be undone.`,
    );
    if (!ok) return;
    const { error } = await api.DELETE("/api/worlds/{world_id}", {
      params: { path: { world_id: world.id } },
    });
    if (error) {
      setError("Could not delete the world. Try again.");
      return;
    }
    void router.navigate({ to: "/" });
  }

  if (world === undefined) {
    return (
      <main>
        <div className="crumb">
          <Link to="/">worlds</Link>
        </div>
        {error ? <p role="alert">{error}</p> : <p role="status">Loading...</p>}
      </main>
    );
  }
  if (world === null) {
    return (
      <main>
        <div className="crumb">
          <Link to="/">worlds</Link>
        </div>
        <h1>No such world</h1>
        <p className="sub">
          It may have been deleted. <Link to="/">Back to your worlds.</Link>
        </p>
      </main>
    );
  }

  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> / {world.name.toLowerCase()}
          </div>
          <h1>{world.name}</h1>
          <p className="sub">{world.purpose || "No purpose written yet."}</p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <section className="tiles" aria-label="Overview">
        <div className="tile" data-testid="needs-you">
          <b>Needs you</b>
          {gates.length === 0 ? (
            "Nothing right now. Open gates land here."
          ) : (
            <>
              {gates.length === 1 ? "One run is waiting on you. " : `${gates.length} runs are waiting on you. `}
              <Link to="/gates">Answer</Link>
            </>
          )}
        </div>
        <div className="tile">
          <b>Workflows</b>
          {workflows.length === 0 ? "None yet." : `${workflows.length} saved, each versioned.`}
        </div>
        <div className="tile">
          <b>Runs</b>
          {runIds.size === 0 ? "None yet. Start one from a workflow below." : `${runIds.size} so far. `}
          {runIds.size > 0 && (
            <Link to="/worlds/$worldId/runs" params={{ worldId }}>
              See runs
            </Link>
          )}
        </div>
        <div className="tile">
          <b>Memory</b>The graph grows from every run's notes.{" "}
          <Link to="/worlds/$worldId/graph" params={{ worldId }}>
            Open Middle-out
          </Link>
        </div>
        <div className="tile">
          <b>Library</b>Every artifact across runs; pin one or start a run from it.{" "}
          <Link to="/worlds/$worldId/library" params={{ worldId }}>
            Open the library
          </Link>
        </div>
        <div className="tile">
          <b>Connectors</b>Web search and the graph are ready.{" "}
          <Link to="/worlds/$worldId/connectors" params={{ worldId }}>
            Manage connectors
          </Link>
        </div>
        <div className="tile">
          <b>Cast</b>Richard plans, the workers claim tasks, Erlich judges; see their records.{" "}
          <Link to="/worlds/$worldId/cast" params={{ worldId }}>
            Meet the cast
          </Link>
        </div>
        <div className="tile">
          <b>Settings</b>Monthly budget, runs at once, file retention, workers, Slack.{" "}
          <Link to="/worlds/$worldId/settings" params={{ worldId }}>
            Open settings
          </Link>
        </div>
      </section>
      <form className="card goal" onSubmit={(e) => void startAdHoc(e)} aria-label="Start from a goal">
        <h2>Start from a goal</h2>
        <p className="faint">
          No recipe needed. One swarm stage works your goal with every connector this world allows,
          and what it learns feeds the graph like any run.
        </p>
        <label>
          Goal
          <textarea
            required
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
            placeholder="Find five hooks for next week's short."
          />
        </label>
        <label>
          Done when (optional)
          <input
            value={doneWhen}
            onChange={(e) => setDoneWhen(e.target.value)}
            placeholder="The goal is met and the findings are written down."
          />
        </label>
        <label>
          Budget in USD (optional)
          <input
            type="number"
            min="0.01"
            step="0.01"
            value={goalBudget}
            onChange={(e) => setGoalBudget(e.target.value)}
            placeholder="5"
          />
        </label>
        <div className="row">
          <button type="submit" className="primary" disabled={starting !== null}>
            {starting === "ad-hoc" ? "Starting..." : "Run this goal"}
          </button>
        </div>
      </form>
      <section aria-label="Workflows" className="workflows">
        <div className="row">
          <h2>Workflows</h2>
          <Link to="/worlds/$worldId/workflows/new" params={{ worldId }} className="button">
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
            <fieldset aria-label="Overrides">
              <legend className="faint">For the next run you start below; blank keeps the workflow's</legend>
              <label>
                Run budget in USD
                <input type="number" min="0.01" step="0.01" value={runBudget} onChange={(e) => setRunBudget(e.target.value)} />
              </label>
              <label>
                Workers per swarm stage
                <input type="number" min="1" max="8" step="1" value={agents} onChange={(e) => setAgents(e.target.value)} />
              </label>
            </fieldset>
            <ul className="workflow-list" data-testid="workflow-list">
              {workflows.map((w) => (
                <li key={w.id}>
                  <Link
                    to="/worlds/$worldId/workflows/$workflowId"
                    params={{ worldId, workflowId: w.id }}
                  >
                    {w.name}
                  </Link>
                  {w.violations?.slice(0, 1).map((v) => (
                    <span key={v.rule} className="chip warn" data-testid={`fails-check-${w.id}`}>
                      Fails today's check: {v.message}. Save a new version to run it.
                    </span>
                  ))}
                  <span className="actions">
                    <span className="faint">v{w.current_version}</span>
                    <Link
                      to="/worlds/$worldId/workflows/$workflowId/scorecard"
                      params={{ worldId, workflowId: w.id }}
                      className="faint"
                      aria-label={`Scorecard of ${w.name}`}
                    >
                      Scorecard
                    </Link>
                    <button
                      type="button"
                      onClick={() => start(w.id)}
                      disabled={starting !== null}
                      aria-label={`Start a run of ${w.name}`}
                    >
                      {starting === w.id ? "Starting..." : "Start run"}
                    </button>
                    <button
                      type="button"
                      onClick={() => start(w.id, true)}
                      disabled={starting !== null}
                      aria-label={`Start a Hooli baseline of ${w.name}`}
                      title="One agent and no graph, so the swarm's runs have something to beat"
                    >
                      Hooli baseline
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>
      <section aria-label="Activity">
        <h2>Activity</h2>
        <ul className="activity" data-testid="activity">
          {collapse(activity).map((a) => (
            <li key={a.seq}>
              <time dateTime={a.ts}>{new Date(a.ts).toLocaleTimeString()}</time>
              <span>{inWords(a.type)}</span>
              {a.times > 1 && <span className="count">{a.times} times</span>}
            </li>
          ))}
        </ul>
      </section>
      <footer className="danger-zone">
        <span className="faint">Deleting removes this world's folder, rows and memory.</span>
        <button type="button" className="danger" onClick={remove}>
          Delete world
        </button>
      </footer>
    </main>
  );
}
