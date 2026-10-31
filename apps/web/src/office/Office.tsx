import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { type ReactNode, useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { type Connectors, useConnectors } from "../api/connectors";
import { type Agent, useWorldEvents } from "../api/events";
import { useOpenGates } from "../api/gates";
import { type Gate, holdsSlot, money, type Run, STATUS_LABEL } from "../api/runs";
import type { components } from "../api/schema";
import { cap, saveSkin } from "../skin";
import { Actor, Bench, type Look, type Pack, still, stillFrame, useCast } from "./cast";
import "./office.css";
import { PixelRoom, Prop, Table, usePixel } from "./pixel";
import {
  agentNews,
  artifactNews,
  asker,
  benchPose,
  bubble,
  crew,
  fileName,
  kindLine,
  kindOf,
  line as lineAt,
  podName,
  podOf,
  restName,
  scene,
  type Seat,
  stageLine,
} from "./scene";

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
  const [agents, setAgents] = useState<Agent[]>([]);
  // The agents as of the latest event, so two updates in one batch compare against each other.
  const latest = useRef<Agent[]>([]);
  const putAgents = (next: Agent[]) => {
    latest.current = next;
    setAgents(next);
  };
  // Per pod (by run id): the latest log line, one bubble per pod, and the latest artifact made.
  const [lines, setLines] = useState<Record<string, { agent: string; text: string }>>({});
  const [made, setMade] = useState<Record<string, { name: string; n: number }>>({});
  const [radio, setRadio] = useState<News[]>([]);
  const { cast, look, nameOf, pack } = useCast(worldId, world?.agent_count, world?.skin);
  const gates = useOpenGates().gates;
  const hear = (who: string, what: string | null) =>
    what && setRadio((prev) => [{ who, what, at: new Date().toTimeString().slice(0, 5), n: (prev[0]?.n ?? 0) + 1 }, ...prev].slice(0, 8));

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
      .then(({ data }) => {
        if (!data) return;
        setRuns(data);
        // Who is at work, and who just shipped: the agents of the live runs and the shelf. Later
        // changes come on the stream. ponytail: one read per run; a world agents read if it grows.
        const { live, shelf } = scene(data);
        return Promise.all(
          [...live, ...shelf].map((r) =>
            api.GET("/api/runs/{run_id}/agents", { params: { path: { run_id: r.id } } }).then(({ data }) => data ?? []),
          ),
        ).then((all) => {
          const prev = latest.current;
          putAgents([...prev, ...all.flat().filter((a) => !prev.some((p) => p.id === a.id))]);
        });
      })
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
    "agent.updated": (agent) => {
      const prev = latest.current;
      const was = prev.find((a) => a.id === agent.id);
      putAgents(was ? prev.map((a) => (a.id === agent.id ? agent : a)) : [...prev, agent]);
      const news = agentNews(agent, was, runs);
      if (!news) return;
      hear(agent.name, news);
      // The agent moved: their line leaves the pod with them.
      setLines((lines) => {
        if (lines[agent.run_id]?.agent !== agent.name) return lines;
        const rest = { ...lines };
        delete rest[agent.run_id];
        return rest;
      });
    },
    // A bubble is for someone seated in the pod, so a line counts only at its own top-level run;
    // child run agents are not seated on the parent's pod.
    "agent.logged": (entry) => {
      const pod = runs.find((r) => !r.parent_run_id && r.stages.some((s) => s.id === entry.stage_id));
      const text = bubble(entry);
      if (pod && text) setLines((prev) => ({ ...prev, [pod.id]: { agent: entry.agent, text } }));
    },
    "artifact.created": (artifact) => {
      const pod = podOf(artifact.stage_id, runs);
      if (pod) setMade((prev) => ({ ...prev, [pod.id]: { name: fileName(artifact.path), n: (prev[pod.id]?.n ?? 0) + 1 } }));
      hear(artifact.author, artifactNews(artifact));
    },
    // A setting changed elsewhere (Settings in another tab): the name and run cap follow it.
    "world.updated": (changes) => setWorld((prev) => prev && { ...prev, ...changes }),
  });

  // A gate in a child run is asked by that run's agents, as Talk shows: read each gate run's once.
  const asked = useRef(new Set<string>());
  useEffect(() => {
    for (const runId of new Set(lineAt(gates, worldId).map((g) => g.run_id))) {
      if (asked.current.has(runId)) continue;
      asked.current.add(runId);
      api
        .GET("/api/runs/{run_id}/agents", { params: { path: { run_id: runId } } })
        .then(({ data }) => {
          const prev = latest.current;
          putAgents([...prev, ...(data ?? []).filter((a) => !prev.some((p) => p.id === a.id))]);
        })
        .catch(() => undefined);
    }
  }, [gates, worldId]);

  async function plainView() {
    if (world && (await saveSkin(worldId, { ...world.skin, view: "plain" }))) void navigate({ to: "/worlds/$worldId", params: { worldId } });
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
  const planner = cast.find((m) => m.role === "planner")?.name;
  const seats = (run: Run) =>
    crew(run, agents).flatMap((seat) => {
      const seen = look(seat.agent.name);
      return seen ? [{ ...seat, look: seen }] : [];
    });
  const waiting = lineAt(gates, worldId).map((gate) => {
    const who = asker(gate, runs, agents, planner);
    return { gate, name: who ? nameOf(who) : "Someone", look: who ? look(who) : null, who };
  });
  const busy = new Set([
    ...scene(runs).live.flatMap((r) => crew(r, agents).map((s) => s.agent.name)),
    ...waiting.flatMap((w) => (w.who ? [w.who] : [])),
  ]);
  const bench = cast.flatMap((m) => {
    const seen = busy.has(m.name) ? null : look(m.name);
    return seen ? [{ name: m.name, look: seen, pose: benchPose(m.name, agents, runs) }] : [];
  });
  const open = (runId: string) => void navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId } });
  const talk = (gate?: string) => void navigate({ to: "/worlds/$worldId/office/talk", params: { worldId }, search: gate ? { gate } : {} });
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
        <button type="button" className="office-chip office-needs" data-testid="office-waiting" onClick={() => talk()}>
          {waiting.length} <span>{waiting.length === 1 ? "decision" : "decisions"} waiting on you</span>
        </button>
        <Link className="office-btn" to="/worlds/$worldId/office/crew" params={{ worldId }}>
          Crew
        </Link>
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
        <Room name={world?.name ?? ""} pack={pack} runs={runs} slots={world?.run_cap ?? 0} connectors={connectors} open={open} seats={seats} lines={lines} made={made}>
          <YourOffice waiting={waiting} talk={talk} />
          <Bench worldId={worldId} members={bench} />
        </Room>
        <div className="office-side">
          <QuestBoard runs={runs} connectors={connectors} open={open} />
          <Radio news={radio} nameOf={nameOf} />
        </div>
      </div>
    </div>
  );
}

type Floor = { runs: Run[]; connectors: Connectors; open: (runId: string) => void };
type News = { who: string; what: string; at: string; n: number };
type Pods = {
  seats: (run: Run) => (Seat & { look: Look })[];
  lines: Record<string, { agent: string; text: string }>;
  made: Record<string, { name: string; n: number }>;
};

type Asker = { gate: Gate; name: string; look: Look | null };

/** Your office, with the askers of the open gates lined up at its door, oldest first. The office
 * and each asker open Talk. */
function YourOffice({ waiting, talk }: { waiting: Asker[]; talk: (gate?: string) => void }) {
  return (
    <div className="office-mine">
      <button type="button" className="office-glass" aria-label={`Your office, ${waiting.length} waiting`} onClick={() => talk()}>
        <b>Your office</b>
        <small>{waiting.length} waiting</small>
      </button>
      <ol className="office-line" aria-label="Line at Your office">
        {waiting.map(({ gate, name, look }) => (
          <li key={gate.stage_id}>
            <button type="button" className="office-asker" aria-label={`${name} asks about ${gate.run_name}: ${gate.ask}`} onClick={() => talk(gate.stage_id)}>
              {look && <img src={stillFrame(look, "talk")} alt="" />}
              <span className="office-tag">{name}</span>
              <q className="office-bubble office-alert">!</q>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** The radio: what just happened, newest first, in the cast's names. */
function Radio({ news, nameOf }: { news: News[]; nameOf: (name: string) => string }) {
  return (
    <section className="office-radio" aria-label="Radio">
      <h2>Radio</h2>
      {news.length === 0 ? (
        <p>Quiet for now.</p>
      ) : (
        <ol>
          {news.map((item) => (
            <li key={item.n}>
              <time>{item.at}</time> <b>{nameOf(item.who)}</b> {item.what}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

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

/** The office: a back wall with windows, the world's sign and a shelf of finished runs over a
 * floor of pods, one per run slot, and the lobby sofa of queued runs. The cast stands on the
 * floor. A pixel pack draws it as the flat-on pixel room, a cartoon pack as the cartoon room. The
 * drawing is decorative; the whiteboards, cups and sofa notes are the controls. */
function Room({ name, pack, ...floor }: Floor & Pods & { name: string; pack: Pack | null; slots: number; children?: ReactNode }) {
  if (pack?.room?.style !== "pixel") {
    return (
      <section className="office-room" aria-label="The office">
        <CartoonBackdrop name={name} />
        <OnTheFloor {...floor} />
      </section>
    );
  }
  return (
    <section className="office-room office-pixel" aria-label="The office">
      <PixelRoom pack={pack} name={name}>
        <OnTheFloor {...floor} />
      </PixelRoom>
    </section>
  );
}

/** The cartoon room's back wall and plank floor, seen at a slight angle. */
const CartoonBackdrop = ({ name }: { name: string }) => (
  <>
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
  </>
);

/** What stands in the room in either style: the shelf, the pods, the lobby sofa and the cast. */
function OnTheFloor({ runs, slots, connectors, open, children, ...pods }: Floor & Pods & { slots: number; children?: ReactNode }) {
  const { live, queued, shelf } = scene(runs);
  // ponytail: every live run gets a pod even past the run cap (the cap was just lowered); that
  // state is short-lived, so no overflow onto the quest board until it proves to matter.
  const spare = Math.max(0, slots - live.length);
  return (
    <>
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
            <Pod run={r} runs={runs} connectors={connectors} open={open} {...pods} />
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
        <Sofa />
      </div>
      {children}
    </>
  );
}

const Sofa = () => (usePixel() ? <Prop name="sofa" /> : <div className="office-sofa" aria-hidden="true" />);

/** One live run's pod: its whiteboard opens the run's plain page; its crew sits at the desks,
 * which glow while a swarm works and shut when the run waits elsewhere. One bubble per pod, from
 * the latest log line of someone in it; the latest artifact made pops up over the desks. */
function Pod({ run, runs, connectors, open, seats, lines, made }: Floor & Pods & { run: Run }) {
  const kind = kindOf(run);
  const crew = seats(run);
  const line = lines[run.id];
  const art = made[run.id];
  // The last of two or more workers roams to the whiteboard and back.
  const roamer = crew.filter((s) => s.pose === "work").at(1)?.agent.id;
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
      {crew.length > 0 && (
        <ul className="office-crew" aria-label={`Crew of ${run.name}`}>
          {crew.map((s) => (
            // A new key when the agent moves to another stage, so they walk in again.
            <Actor
              key={`${s.agent.id}/${s.agent.stage_id}`}
              look={s.look}
              pose={s.pose}
              roam={s.agent.id === roamer}
              say={s.pose === "talk" ? "!" : line?.agent === s.agent.name ? line.text : null}
            />
          ))}
        </ul>
      )}
      {/* With reduced motion there is no pop; the radio carries the news. */}
      {art && !still() && (
        <span key={art.n} className="office-pop">
          {art.name}
        </span>
      )}
      <Desks on={kind === "swarm" || kind === "retro"} />
      {kind === "connector" && <Cart />}
    </>
  );
}

function Desks({ on }: { on: boolean }) {
  if (usePixel()) return <Table on={on} />;
  return (
    <div className={`office-desks${on ? " office-on" : ""}`} aria-hidden="true">
      <i />
      <i />
    </div>
  );
}

const Cart = () => (usePixel() ? <Prop name="cart" /> : <div className="office-cart" aria-hidden="true" />);
