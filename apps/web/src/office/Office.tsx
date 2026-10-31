import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { useWorldEvents } from "../api/events";
import { holdsSlot, money, type Run } from "../api/runs";
import type { components } from "../api/schema";
import { saveView } from "../skin";
import "./office.css";

type World = components["schemas"]["World"];
type Spend = components["schemas"]["Spend"];

/** A world as a startup office: its own full-screen shell with a top bar of the vital figures
 * over the room. Only the API and the skin module come in here (ADR 0002). */
export function Office() {
  const { worldId } = useParams({ from: "/worlds/$worldId/office" });
  const navigate = useNavigate();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState<string | null>(null);

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
        <button type="button" className="office-btn" onClick={() => void plainView()}>
          Plain view
        </button>
      </header>
      {error && (
        <p className="office-note" role="alert">
          {error}
        </p>
      )}
      <Room name={world?.name ?? ""} />
    </div>
  );
}

/** The empty office in the cartoon style: a back wall with windows and the world's sign over a
 * plank floor. Decorative; the controls around it carry the meaning. */
function Room({ name }: { name: string }) {
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
    </section>
  );
}
