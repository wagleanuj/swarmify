import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, useContext, useEffect, useState } from "react";

import { api } from "../api/client";
import { money } from "../api/runs";
import type { components } from "../api/schema";
import { CurrentWorld } from "../Shell";

type World = components["schemas"]["World"];

// The eval harness makes a world per trial ("Eval <task> <arm> <n>") and keeps its reports in "Evals".
const isEval = (w: World) => w.name === "Evals" || w.name.startsWith("Eval ");

export function Worlds() {
  const router = useRouter();
  const current = useContext(CurrentWorld);
  const [worlds, setWorlds] = useState<World[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setWorlds(data);
      })
      .catch(() => setError("Could not load worlds. Is the API running?"));
  }, []);

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const fields = new FormData(form);
    const name = String(fields.get("name") ?? "").trim();
    if (!name) return;
    setBusy(true);
    setError(null);
    const { data, error } = await api.POST("/api/worlds", {
      body: { name, purpose: String(fields.get("purpose") ?? "").trim() },
    });
    setBusy(false);
    if (error || !data) {
      setError("Could not create the world. Try again.");
      return;
    }
    form.reset();
    void router.navigate({ to: "/worlds/$worldId", params: { worldId: data.id } });
  }

  const own = worlds?.filter((w) => !isEval(w)) ?? [];
  const evals = worlds?.filter(isEval) ?? [];
  const head = (
    <thead>
      <tr>
        <th>World</th>
        <th className="num">Created</th>
        <th className="num">Run cap</th>
        <th className="num">Monthly budget</th>
      </tr>
    </thead>
  );
  const row = (w: World) => (
    <tr key={w.id} data-testid="world-row">
      <td>
        <Link to="/worlds/$worldId" params={{ worldId: w.id }}>
          {w.name}
        </Link>
        {w.id === current && (
          <>
            {" "}
            <span className="pill current">Current</span>
          </>
        )}
      </td>
      <td className="num">{new Date(w.created_at).toLocaleDateString()}</td>
      <td className="num">{w.run_cap ?? 2}</td>
      <td className="num">{w.monthly_budget_usd == null ? "No ceiling" : money(w.monthly_budget_usd)}</td>
    </tr>
  );

  return (
    <main>
      <header className="page">
        <div>
          <h1>Your worlds</h1>
          <p className="sub">
            {worlds && worlds.length > 0
              ? `${own.length} ${own.length === 1 ? "world" : "worlds"}${evals.length ? `, plus ${evals.length} made by evals` : ""}. Each one remembers on its own.`
              : "A world is one long memory and the connectors it may use. Runs come and go. The world remembers."}
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="worlds-layout">
        <section className="panel" aria-label="Worlds">
          {worlds?.length === 0 ? (
            <p className="faint" data-testid="empty">
              Nothing here yet. Give each kind of work its own world so Video never learns from Market research.
            </p>
          ) : (
            <table className="scores">
              {head}
              <tbody>{own.map(row)}</tbody>
            </table>
          )}
          {evals.length > 0 && (
            <details className="eval-worlds" data-testid="eval-worlds">
              <summary>Eval worlds ({evals.length})</summary>
              <table className="scores">
                {head}
                <tbody>{evals.map(row)}</tbody>
              </table>
            </details>
          )}
        </section>
        <form className="panel" onSubmit={create} aria-label="New world">
          <div>
            <h2>New world</h2>
            <p className="sub">A world keeps its own workflows, library, memory and budget.</p>
          </div>
          <label className="field">
            Name
            <input name="name" required maxLength={120} placeholder="Video" autoComplete="off" />
          </label>
          <label className="field">
            What is this world about?
            <input
              name="purpose"
              maxLength={500}
              placeholder="Weekly shorts for the attraction channel."
              autoComplete="off"
            />
          </label>
          <div>
            <button type="submit" className="primary" disabled={busy}>
              {busy ? "Creating..." : "Create world"}
            </button>
          </div>
        </form>
      </div>
    </main>
  );
}
