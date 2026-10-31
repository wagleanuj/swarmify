import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { type ReactNode, useEffect, useState } from "react";

import { api } from "../api/client";
import { type Connectors, useConnectors } from "../api/connectors";
import { useWorldEvents } from "../api/events";
import { holdsSlot, money, type Run, STATUS_LABEL } from "../api/runs";
import type { components } from "../api/schema";
import { cap, saveView } from "../skin";
import { StandingCast } from "./cast";
import "./office.css";
import { kindLine, kindOf, podName, restName, scene, stageLine } from "./scene";

type World = components["schemas"]["World"];
type Spend = components["schemas"]["Spend"];
type Score = components["schemas"]["RunScore"];

/** A world as a startup office: its own full-screen shell with a top bar of the vital figures
 * over the room. Only the API and the skin module come in here (ADR 0002). */
export function Office() {
  const { worldId } = useParams({ from: "/worlds/$worldId/office" });
  const navigate = useNavigate();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [scores, setScores] = useState<Score[]>([]);
  const [error, setError] = useState<string | null>(null);
  const connectors = useConnectors(worldId);

  useEffect(() => {
    const path = { params: { path: { world_id: worldId } } };
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data, response }) => {
        if (response.status === 404) return setWorld(null);
        if (!data) throw new Error("load failed");
        setWorld(data);
      })
      .catch(() => setError("Could not load this world. Is the API running?"));
    // ponytail: spend is read when the office opens; follow it live once the office shows costs.
    api
      .GET("/api/worlds/{world_id}/spend", path)
      .then(({ data }) => data && setSpend(data))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/runs", path)
      .then(({ data }) => data && setRuns(data))
      .catch(() => undefined);
    // ponytail: first-try keeps are read when the office opens, one scorecard per workflow, as
    // the plain Scorecard page sums them; a world scorecard endpoint would make it one read.
    api
      .GET("/api/worlds/{world_id}/workflows", path)
      .then(({ data }) =>
        Promise.all(
          (data ?? []).map((w) =>
            api
              .GET("/api/workflows/{workflow_id}/scorecard", { params: { path: { workflow_id: w.id } } })
              .then(({ data }) => data?.runs ?? []),
          ),
        ),
      )
      .then((cards) => setScores(cards.flat()))
      .catch(() => undefined);
  }, [worldId]);

  useWorldEvents(worldId, {
    "run.updated": (run) =>
      setRuns((prev) => (prev.some((r) => r.id === run.id) ? prev.map((r) => (r.id === run.id ? run : r)) : [run, ...prev])),
    // A setting changed elsewhere (Settings in another tab): the name and run cap follow it.
    "world.updated": (changes) => setWorld((prev) => prev && { ...prev, ...changes }),
  });

  async function plainView() {
    if (await saveView(worldId, "plain")) void navigate({ to: "/worlds/$worldId", params: { worldId } });
    else setError("Could not switch to the plain view. Try again.");
  }

  if (world === null) {
    return (
      <div className="office-app">
        <p className="office-note" role="alert">
          No such world. <Link to="/">Back to your worlds.</Link>
        </p>
      </div>
    );
  }

  const budget = spend?.monthly_budget_usd;
  const open = (runId: string) => void navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId } });
  return (
    <div className="office-app">
      <header className="office-bar" aria-label="Office">
        <div className="office-chip office-world">
          <small>World</small>
          <h1>{world?.name ?? "..."}</h1>
        </div>
        <div className="office-chip" data-testid="office-spend">
          {spend ? money(spend.spent_usd) : "..."}{" "}
          <span>{budget == null ? "spent, no monthly budget" : `of ${money(budget)} this month`}</span>
        </div>
        <div className="office-chip" data-testid="office-runs">
          {runs.filter(holdsSlot).length} <span>of {world?.run_cap ?? "..."} runs going</span>
        </div>
        <div className="office-chip" data-testid="office-first-try">
          {scores.reduce((n, s) => n + s.first_try, 0)}{" "}
          <span>of {scores.reduce((n, s) => n + s.judged, 0)} kept first try</span>
        </div>
        <button type="button" className="office-btn" onClick={() => void plainView()}>
          Plain view
        </button>
      </header>
      {error && (
        <p className="office-note" role="alert">
          {error}
        </p>
      )}
      <div className="office-main">
        <Room name={world?.name ?? ""} runs={runs} slots={world?.run_cap ?? 0} connectors={connectors} open={open}>
          {world && <StandingCast worldId={worldId} agentCount={world.agent_count} />}
        </Room>
        <QuestBoard runs={runs} connectors={connectors} open={open} />
      </div>
    </div>
  );
}

type Floor = { runs: Run[]; connectors: Connectors; open: (runId: string) => void };

/** The readable list beside the picture: live, queued and recently finished runs. */
function QuestBoard({ runs, connectors, open }: Floor) {
  const { live, queued, shelf } = scene(runs);
  const note = (run: Run, label: string, meta: string, kind: string) => (
    <li key={run.id}>
      <button type="button" className={`office-note-card office-${kind}`} aria-label={label} onClick={() => open(run.id)}>
        <b>{run.name}</b>
        <small>{meta}</small>
      </button>
    </li>
  );
  return (
    <aside className="office-quests" aria-label="Quest board">
      <h2>Quest board</h2>
      <ul>
        {live.map((r) => note(r, podName(r, runs, connectors), `${cap(stageLine(r))} · ${kindLine(r, runs, connectors)}`, kindOf(r)))}
        {queued.map((r) => note(r, restName(r), "Queued, waiting for a free pod", "queued"))}
        {shelf.map((r) => note(r, restName(r), cap(STATUS_LABEL[r.status]), r.status))}
      </ul>
      {live.length + queued.length + shelf.length === 0 && <p>No runs yet today.</p>}
    </aside>
  );
}

/** The office in the cartoon style: a back wall with windows, the world's sign and a shelf of
 * finished runs over a plank floor of pods, one per run slot, and the lobby sofa of queued runs.
 * The cast stands on the floor. The drawing is decorative; the whiteboards, cups and sofa notes
 * are the controls. */
function Room({ name, runs, slots, connectors, open, children }: Floor & { name: string; slots: number; children?: ReactNode }) {
  const { live, queued, shelf } = scene(runs);
  // ponytail: every live run gets a pod even past the run cap (the cap was just lowered); that
  // state is short-lived, so no overflow onto the quest board until it proves to matter.
  const spare = Math.max(0, slots - live.length);
  return (
    <section className="office-room" aria-label="The office">
      <div className="office-wall" aria-hidden="true">
        <div className="office-windows">
          {Array.from({ length: 5 }, (_, i) => (
            <i key={i} />
          ))}
        </div>
        <div className="office-neon">{name}</div>
        <div className="office-entry">
          <span>IN</span>
          <i />
          <i />
        </div>
        <div className="office-kitchen">
          <div className="office-fridge" />
          <div className="office-counter" />
        </div>
      </div>
      <div className="office-floor" aria-hidden="true" />
      <ul className="office-shelf" aria-label="Shelf">
        {shelf.map((r) => (
          <li key={r.id}>
            <button type="button" className={`office-cup office-${r.status}`} aria-label={restName(r)} title={restName(r)} onClick={() => open(r.id)} />
          </li>
        ))}
      </ul>
      <ul className="office-pods" aria-label="Pods">
        {live.map((r) => (
          <li key={r.id} className={`office-pod office-${kindOf(r)}`}>
            <Pod run={r} runs={runs} connectors={connectors} open={open} />
          </li>
        ))}
        {Array.from({ length: spare }, (_, i) => (
          <li key={`spare-${i}`} className="office-pod office-spare">
            <div className="office-board">Free pod</div>
            <Desks on={false} />
          </li>
        ))}
      </ul>
      <div className="office-lobby" aria-label="Lobby sofa" role="group">
        <ol>
          {queued.map((r) => (
            <li key={r.id}>
              <button type="button" className="office-note-card office-queued" aria-label={restName(r)} onClick={() => open(r.id)}>
                <small>Queued, waiting for a free pod</small>
                <b>{r.name}</b>
              </button>
            </li>
          ))}
        </ol>
        <div className="office-sofa" aria-hidden="true" />
      </div>
      {children}
    </section>
  );
}

/** One live run's pod: its whiteboard opens the run's plain page; the desks glow while a swarm
 * works and shut when the run waits elsewhere. */
function Pod({ run, runs, connectors, open }: Floor & { run: Run }) {
  const kind = kindOf(run);
  const done = run.stages.filter((s) => s.status === "done").length;
  return (
    <>
      <button type="button" className="office-board" aria-label={podName(run, runs, connectors)} onClick={() => open(run.id)}>
        <b>{run.name}</b>
        <small>{cap(stageLine(run))}</small>
        <span className="office-progress" aria-hidden="true">
          <i style={{ width: `${(done / Math.max(1, run.stages.length)) * 100}%` }} />
        </span>
        <span className="office-kind">{kindLine(run, runs, connectors)}</span>
      </button>
      <Desks on={kind === "swarm" || kind === "retro"} />
      {kind === "connector" && <div className="office-cart" aria-hidden="true" />}
    </>
  );
}

const Desks = ({ on }: { on: boolean }) => (
  <div className={`office-desks${on ? " office-on" : ""}`} aria-hidden="true">
    <i />
    <i />
  </div>
);
