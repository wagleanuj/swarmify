import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import { useWorldEvents } from "../api/events";
import type { components } from "../api/schema";
import { libraryRoute } from "../router";

type World = components["schemas"]["World"];
type Workflow = components["schemas"]["Workflow"];
type Item = components["schemas"]["LibraryItem"];
type StageDef = components["schemas"]["WorkflowModel"]["stages"][number];

const ANY = "";
type Filters = { type: string; workflow: string; stage: string; run: string; from: string; to: string };
const NONE: Filters = { type: ANY, workflow: ANY, stage: ANY, run: ANY, from: ANY, to: ANY };

/** The items that pass every set filter; dates compare on the calendar day. */
export function filter(items: Item[], f: Filters): Item[] {
  return items.filter(
    (i) =>
      (!f.type || i.type === f.type) &&
      (!f.workflow || i.workflow_id === f.workflow) &&
      (!f.stage || i.stage_name === f.stage) &&
      (!f.run || i.run_id === f.run) &&
      (!f.from || i.created.slice(0, 10) >= f.from) &&
      (!f.to || i.created.slice(0, 10) <= f.to),
  );
}

/** Every artifact of the world across its runs (Memory's Library), with pins and promotes. */
export function Library() {
  const { worldId } = libraryRoute.useParams();
  const router = useRouter();
  const [world, setWorld] = useState<World | null>(null);
  const [items, setItems] = useState<Item[] | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [filters, setFilters] = useState<Filters>(NONE);
  const [error, setError] = useState<string | null>(null);
  const [seen, setSeen] = useState(0);
  const [promoting, setPromoting] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorld(data ?? null))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorkflows(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/library", { params: { path: { world_id: worldId } } })
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setItems(data);
      })
      .catch(() => setError("Could not load the library. Is the API running?"));
  }, [worldId, seen]);

  useWorldEvents(worldId, {
    "artifact.created": (created) => {
      if (created.type !== "note") setSeen((n) => n + 1);
    },
  });

  async function pin(item: Item) {
    setError(null);
    const { data, error } = await api.POST("/api/worlds/{world_id}/pins", {
      params: { path: { world_id: worldId } },
      body: { path: item.path, pinned: !item.pinned },
    });
    if (error || !data) return setError(`Could not ${item.pinned ? "unpin" : "pin"} ${item.title}. Try again.`);
    setItems((prev) => (prev ?? []).map((i) => (i.path === data.path ? data : i)));
  }

  const set = (key: keyof Filters) => (value: string) => setFilters((f) => ({ ...f, [key]: value }));
  const shown = filter(items ?? [], filters);
  const active = Object.values(filters).some(Boolean);
  const options = (pick: (i: Item) => [string, string]) =>
    [...new Map((items ?? []).map(pick)).entries()].sort((a, b) => a[1].localeCompare(b[1]));

  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> /{" "}
            <Link to="/worlds/$worldId" params={{ worldId }}>
              {world?.name.toLowerCase() ?? worldId}
            </Link>{" "}
            / library
          </div>
          <h1>Library</h1>
          <p className="sub">
            Everything this world's runs have made: ideas, scripts, verdicts, decisions, files. Pin
            one and Richard and Gilfoyle read it before every later stage. Promote one to start a
            workflow from it without redoing the earlier stages.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {items === null && !error && <p role="status">Loading...</p>}
      {items?.length === 0 && (
        <div className="empty" data-testid="no-artifacts">
          <p>Nothing yet. Artifacts land here as runs finish stages, newest first.</p>
          <Link to="/worlds/$worldId/runs" params={{ worldId }} className="button">
            See runs
          </Link>
        </div>
      )}
      {items && items.length > 0 && (
        <>
          <form className="filters" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
            <Select label="Type" value={filters.type} onChange={set("type")} options={options((i) => [i.type, i.type])} />
            <Select
              label="Workflow"
              value={filters.workflow}
              onChange={set("workflow")}
              options={options((i) => [i.workflow_id, workflows.find((w) => w.id === i.workflow_id)?.name ?? i.run_name])}
            />
            <Select label="Stage" value={filters.stage} onChange={set("stage")} options={options((i) => [i.stage_name, i.stage_name])} />
            <Select
              label="Run"
              value={filters.run}
              onChange={set("run")}
              options={options((i) => [i.run_id, `${i.run_name} ${i.run_id.slice(-6)}`])}
            />
            <label>
              From <input type="date" value={filters.from} onChange={(e) => set("from")(e.target.value)} />
            </label>
            <label>
              To <input type="date" value={filters.to} onChange={(e) => set("to")(e.target.value)} />
            </label>
            <span className="faint" data-testid="count">
              {shown.length} of {items.length}
            </span>
            {active && (
              <button type="button" className="link" onClick={() => setFilters(NONE)}>
                Clear filters
              </button>
            )}
          </form>
          {shown.length === 0 && <p className="faint">No artifact matches these filters.</p>}
          <ul className="cards" data-testid="library">
            {shown.map((i) => (
              <li key={i.path} className="card" data-testid="artifact" data-pinned={i.pinned}>
                <span className="chips">
                  <span className="chip">{i.type}</span>
                  {i.pinned && <span className="chip pinned">pinned</span>}
                </span>
                <b>{i.title}</b>
                <span className="faint">
                  from{" "}
                  <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: i.run_id }}>
                    {i.run_name}
                  </Link>
                  {i.stage_index !== null && ` · stage ${i.stage_index + 1} ${i.stage_name}`}
                  {i.workflow_version !== null && ` · v${i.workflow_version}`}
                </span>
                {i.created && (
                  <time className="faint" dateTime={i.created}>
                    {new Date(i.created).toLocaleString()}
                  </time>
                )}
                <span className="chips">
                  <button type="button" onClick={() => pin(i)} aria-label={`${i.pinned ? "Unpin" : "Pin"} ${i.title}`}>
                    {i.pinned ? "Unpin" : "Pin"}
                  </button>
                  <button
                    type="button"
                    aria-expanded={promoting === i.path}
                    aria-label={`Promote ${i.title}`}
                    onClick={() => setPromoting((p) => (p === i.path ? null : i.path))}
                  >
                    Promote...
                  </button>
                </span>
                {promoting === i.path && (
                  <PromoteForm
                    item={i}
                    workflows={workflows}
                    onStarted={(runId) =>
                      void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId } })
                    }
                  />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </main>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <label>
      {label}{" "}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value={ANY}>All</option>
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

/** Start a workflow at a chosen stage with this artifact as the input of the stages before it. */
function PromoteForm({
  item,
  workflows,
  onStarted,
}: {
  item: Item;
  workflows: Workflow[];
  onStarted: (runId: string) => void;
}) {
  const [workflowId, setWorkflowId] = useState(
    workflows.some((w) => w.id === item.workflow_id) ? item.workflow_id : (workflows[0]?.id ?? ""),
  );
  const [stages, setStages] = useState<StageDef[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const workflow = workflows.find((w) => w.id === workflowId);

  useEffect(() => {
    setStages(null);
    if (!workflow) return;
    api
      .GET("/api/workflows/{workflow_id}/versions/{number}", {
        params: { path: { workflow_id: workflow.id, number: workflow.current_version } },
      })
      .then(({ data }) => setStages(data?.model.stages ?? []))
      .catch(() => setError("Could not load the workflow's stages."));
  }, [workflow]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const stage = Number(new FormData(e.currentTarget as HTMLFormElement).get("stage"));
    setBusy(true);
    setError(null);
    const { data, error, response } = await api.POST("/api/workflows/{workflow_id}/promote", {
      params: { path: { workflow_id: workflowId } },
      body: { artifact: item.path, stage },
    });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      return setError(typeof detail === "string" ? detail : `Could not start the run (${response.status}).`);
    }
    onStarted(data.id);
  }

  const starts = (stages ?? []).slice(1);
  return (
    <form className="promote" aria-label={`Promote ${item.title}`} onSubmit={submit}>
      <label>
        Workflow{" "}
        <select value={workflowId} onChange={(e) => setWorkflowId(e.target.value)}>
          {workflows.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name} v{w.current_version}
            </option>
          ))}
        </select>
      </label>
      <label>
        Start at{" "}
        <select name="stage" disabled={stages === null}>
          {starts.map((s, n) => (
            <option key={s.name} value={n + 1}>
              {n + 1}. {s.name}
            </option>
          ))}
        </select>
      </label>
      <span className="faint">
        {starts.length === 0 && stages !== null
          ? "This workflow has one stage, so there is nothing to skip. Start a run instead."
          : `${item.name} stands in for the skipped stages' output.`}
      </span>
      <button type="submit" disabled={busy || !workflow || starts.length === 0}>
        {busy ? "Starting..." : "Start run"}
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
