import { Link, useRouter } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { Gate, Run } from "../api/runs";
import type { components } from "../api/schema";
import { worldRoute } from "../router";

type World = components["schemas"]["World"];
type Workflow = components["schemas"]["Workflow"];
type Activity = { seq: number; type: string; ts: string };

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
    const source = new EventSource(`/api/worlds/${worldId}/events`);
    const onEvent = (e: Event) => {
      const data = JSON.parse((e as MessageEvent<string>).data) as Activity & { payload?: Run };
      const run = data.payload;
      if (data.type === "run.updated" && run) {
        setRunIds((prev) => new Set(prev).add(run.id));
        void loadGates();
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
    return () => source.close();
  }, [worldId]);

  async function start(workflowId: string, baseline = false) {
    setStarting(workflowId);
    setError(null);
    const { data, error } = await api.POST("/api/workflows/{workflow_id}/runs", {
      params: { path: { workflow_id: workflowId } },
      body: { baseline },
    });
    setStarting(null);
    if (error || !data) {
      setError("Could not start the run. Try again.");
      return;
    }
    void router.navigate({
      to: "/worlds/$worldId/runs/$runId",
      params: { worldId, runId: data.id },
    });
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
          <b>Settings</b>Slack tells you when a gate opens or a run ends.{" "}
          <Link to="/worlds/$worldId/settings" params={{ worldId }}>
            Set up Slack
          </Link>
        </div>
      </section>
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
          <ul className="workflow-list" data-testid="workflow-list">
            {workflows.map((w) => (
              <li key={w.id}>
                <Link
                  to="/worlds/$worldId/workflows/$workflowId"
                  params={{ worldId, workflowId: w.id }}
                >
                  {w.name}
                </Link>
                <span className="actions">
                  <span className="faint">v{w.current_version}</span>
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
        )}
      </section>
      <section aria-label="Activity">
        <h2>Activity</h2>
        <ul className="activity" data-testid="activity">
          {activity.map((a) => (
            <li key={a.seq}>
              <time dateTime={a.ts}>{new Date(a.ts).toLocaleTimeString()}</time>
              <span>{a.type}</span>
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
