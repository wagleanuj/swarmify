import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { Health } from "../Health";

type World = components["schemas"]["WorldOut"];

export function Worlds() {
  const router = useRouter();
  const [worlds, setWorlds] = useState<World[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
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

  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">swarmify</div>
          <h1>Your worlds</h1>
          <p className="sub">
            A world is one long memory and the connectors it may use. Runs come and go. The world
            remembers.
          </p>
        </div>
        <Health />
      </header>
      {error && <p role="alert">{error}</p>}
      {worlds?.length === 0 && (
        <p className="faint" data-testid="empty">
          Nothing here yet. Give each kind of work its own world so Video never learns from Market
          research.
        </p>
      )}
      <section className="grid" aria-label="Worlds">
        {worlds?.map((w) => (
          <article className="card" key={w.id} data-testid="world-card">
            <h3>
              <Link to="/worlds/$worldId" params={{ worldId: w.id }}>
                {w.name}
              </Link>
            </h3>
            <p>{w.purpose || "No purpose written yet."}</p>
            <span className="faint">created {new Date(w.created_at).toLocaleDateString()}</span>
          </article>
        ))}
        <form className="card new" onSubmit={create} aria-label="New world">
          <h3>New world</h3>
          <label>
            Name
            <input name="name" required maxLength={120} placeholder="Video" autoComplete="off" />
          </label>
          <label>
            What is this world about?
            <input
              name="purpose"
              maxLength={500}
              placeholder="Weekly shorts for the attraction channel."
              autoComplete="off"
            />
          </label>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? "Creating..." : "Create world"}
          </button>
        </form>
      </section>
    </main>
  );
}
