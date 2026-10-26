import { Link, useRouter } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { worldRoute } from "../router";

type World = components["schemas"]["World"];
type Activity = { seq: number; type: string; ts: string };

export function WorldOverview() {
  const { worldId } = worldRoute.useParams();
  const router = useRouter();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [activity, setActivity] = useState<Activity[]>([]);
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

  // Skins render events, they never poll tables (ADR 0002). Native EventSource, no library.
  useEffect(() => {
    const source = new EventSource(`/api/worlds/${worldId}/events`);
    const onEvent = (e: Event) => {
      const data = JSON.parse((e as MessageEvent<string>).data) as Activity;
      setActivity((prev) => (prev.some((a) => a.seq === data.seq) ? prev : [...prev, data]));
    };
    for (const type of ["world.created", "world.deleted"]) source.addEventListener(type, onEvent);
    return () => source.close();
  }, [worldId]);

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
        <div className="tile">
          <b>Needs you</b>Nothing right now. Open gates land here.
        </div>
        <div className="tile">
          <b>Workflows</b>None yet. The editor arrives with the next release.
        </div>
        <div className="tile">
          <b>Runs</b>None yet.
        </div>
        <div className="tile">
          <b>Memory</b>Empty. The graph grows from the first run's notes.
        </div>
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
