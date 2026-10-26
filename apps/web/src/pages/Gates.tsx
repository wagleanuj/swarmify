import { Link } from "@tanstack/react-router";
import { type FormEvent, useCallback, useEffect, useState } from "react";

import { api } from "../api/client";
import { decide, type Gate } from "../api/runs";
import type { components } from "../api/schema";
import { ChildOf } from "./Runs";

type World = components["schemas"]["World"];

export function Gates() {
  const [gates, setGates] = useState<Gate[] | null>(null);
  const [worlds, setWorlds] = useState<World[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .GET("/api/gates")
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setGates(data);
      })
      .catch(() => setError("Could not load the gates. Is the API running?"));
  }, []);

  useEffect(load, [load]);
  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
      .catch(() => undefined);
  }, []);

  // Gates live across worlds and each world has its own stream, so listen to every one and
  // refetch the inbox whenever a run moves (ADR 0002: skins render events, never tables).
  // ponytail: one EventSource per world, fine for a handful; a cross-world gate stream when
  // worlds pile up (browsers cap HTTP/1.1 connections per host at six).
  useEffect(() => {
    const sources = worlds.map((w) => new EventSource(`/api/worlds/${w.id}/events`));
    for (const s of sources) s.addEventListener("run.updated", load);
    return () => sources.forEach((s) => s.close());
  }, [worlds, load]);

  async function answer(gate: Gate, body: Parameters<typeof decide>[1]) {
    setError(null);
    const refused = await decide(gate.run_id, body);
    if (refused) setError(refused);
    load();
  }

  const worldName = (id: string) => worlds.find((w) => w.id === id)?.name ?? id;

  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> / needs you
          </div>
          <h1>Needs you</h1>
          <p className="sub">
            Every open gate from every world and run, newest first. Answer here and the run moves
            on; your answer is kept as a decision file in the run folder.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {gates === null && !error && <p role="status">Loading...</p>}
      {gates?.length === 0 && (
        <div className="empty" data-testid="no-gates">
          <p>
            Nothing is waiting on you. When a run reaches a gate it lands here, pausing until you
            pick, approve or confirm.
          </p>
          <Link to="/" className="button">
            See your worlds
          </Link>
        </div>
      )}
      {gates && gates.length > 0 && (
        <ul className="gate-list" data-testid="gate-list">
          {gates.map((g) => (
            <GateCard key={g.stage_id} gate={g} world={worldName(g.world_id)} onAnswer={answer} />
          ))}
        </ul>
      )}
    </main>
  );
}

function GateCard({
  gate,
  world,
  onAnswer,
}: {
  gate: Gate;
  world: string;
  onAnswer: (gate: Gate, body: Parameters<typeof decide>[1]) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [sendBack, setSendBack] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fields = new FormData(e.currentTarget);
    const note = String(fields.get("note") ?? "").trim();
    const body: Parameters<typeof decide>[1] = { note };
    if (gate.kind === "pick") body.choice = String(fields.get("choice") ?? "").trim();
    if (sendBack) body.send_back_to = String(fields.get("send_back_to") ?? "");
    setBusy(true);
    await onAnswer(gate, body);
    setBusy(false);
    setSendBack(false);
  }

  const since = new Date(gate.opened_at).toLocaleTimeString([], { timeStyle: "short" });
  return (
    <li className="gate" data-kind={gate.kind} data-testid={`gate-${gate.run_id}`}>
      <div className="gate-head">
        <Link
          to="/worlds/$worldId/runs/$runId"
          params={{ worldId: gate.world_id, runId: gate.run_id }}
        >
          {gate.run_name}
        </Link>
        <span className="faint">
          {world} · stage {gate.stage_index + 1} · {gate.stage_name} · waiting since {since}
        </span>
        {gate.parent && <ChildOf parent={gate.parent} />}
      </div>
      <p className="ask">{gate.ask}</p>
      <form onSubmit={submit} className="gate-form" aria-label={`Answer ${gate.stage_name}`}>
        {gate.kind === "pick" && (
          <>
            <label>
              Your pick
              <input name="choice" required autoComplete="off" placeholder="idea 1" />
            </label>
            <span className="faint">
              Idea cards arrive with the swarm; until then, name your pick.
            </span>
          </>
        )}
        <label>
          {gate.kind === "approve" ? "Note (sent back with it, or kept with the approval)" : "Note (optional)"}
          <textarea name="note" rows={2} />
        </label>
        {gate.kind === "approve" && sendBack && (
          <label>
            Send back to
            <select name="send_back_to" defaultValue={gate.earlier_stages.at(-1)}>
              {gate.earlier_stages.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="row gate-actions">
          {gate.kind === "pick" && (
            <button type="submit" className="primary" disabled={busy}>
              Continue with this pick
            </button>
          )}
          {gate.kind === "confirm" && (
            <>
              <button type="submit" className="primary" disabled={busy}>
                Confirm
              </button>
              <span className="faint">Nothing goes out until you confirm.</span>
            </>
          )}
          {gate.kind === "approve" && !sendBack && (
            <>
              <button type="submit" className="primary" disabled={busy}>
                Approve
              </button>
              {gate.send_backs_left > 0 && gate.earlier_stages.length > 0 ? (
                <>
                  <button type="button" onClick={() => setSendBack(true)} disabled={busy}>
                    Send back...
                  </button>
                  <span className="faint">{gate.send_backs_left} of 2 send-backs left</span>
                </>
              ) : (
                <span className="faint">
                  Sent back twice already. Approve, or abort the run from its timeline.
                </span>
              )}
            </>
          )}
          {gate.kind === "approve" && sendBack && (
            <>
              <button type="submit" className="primary" disabled={busy}>
                Send back
              </button>
              <button type="button" onClick={() => setSendBack(false)} disabled={busy}>
                Cancel
              </button>
              <span className="faint">The stage runs again with your note in its context.</span>
            </>
          )}
        </div>
      </form>
    </li>
  );
}
