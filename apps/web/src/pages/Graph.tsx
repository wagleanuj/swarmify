import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { graphRoute } from "../router";

type World = components["schemas"]["World"];
type View = components["schemas"]["GraphView"];

/** Pied Piper names belong to the skin (ADR 0004): the GraphWriter is Anton. */
const WRITER = "Anton";

function ago(ts: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - new Date(ts).getTime()) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ts).toLocaleDateString();
}

const signed = (n: number) => (n >= 0 ? `+${n}` : `${n}`);

export function Graph() {
  const { worldId } = graphRoute.useParams();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [view, setView] = useState<View | null>(null);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<{ question: string; text: string } | null>(null);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function load() {
    return api
      .GET("/api/worlds/{world_id}/graph", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setView(data));
  }

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data, response }) => setWorld(response.status === 404 ? null : (data ?? null)))
      .catch(() => setError("Could not load this world. Is the API running?"));
    void load().catch(() => undefined);
  }, [worldId]);

  // Rebuilds arrive on the world's stream (ADR 0002); each one reloads the summary. Only new
  // ones matter, so `since=now` skips replaying history.
  useEffect(() => {
    const source = new EventSource(`/api/worlds/${worldId}/events?since=now`);
    const reload = () => void load().catch(() => undefined);
    source.addEventListener("graph.rebuilt", reload);
    source.addEventListener("graph.failed", reload);
    return () => source.close();
  }, [worldId]);

  async function ask(text: string) {
    const q = text.trim();
    if (!q) return;
    setAsking(true);
    setError(null);
    const { data, error } = await api.POST("/api/worlds/{world_id}/graph/query", {
      params: { path: { world_id: worldId } },
      body: { question: q },
    });
    setAsking(false);
    if (error || !data) {
      setError("The graph did not answer. Try again in a moment.");
      return;
    }
    setAnswer({ question: q, text: data.answer });
  }

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    void ask(question);
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
        <h1>No such world</h1>
        <p className="sub">
          It may have been deleted. <Link to="/">Back to your worlds.</Link>
        </p>
      </main>
    );
  }

  const rebuilt = view?.rebuilt;
  const empty = view !== null && view.nodes === 0;
  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> /{" "}
            <Link to="/worlds/$worldId" params={{ worldId }}>
              {world.name.toLowerCase()}
            </Link>{" "}
            / middle-out
          </div>
          <h1>Middle-out</h1>
          <p className="sub">
            Everything this world has learned, as one graph. Agents query it instead of re-reading
            files. You can too.
          </p>
        </div>
        {view && (
          <div className="pills" data-testid="graph-size">
            <span className="pill">{view.nodes.toLocaleString()} nodes</span>
            <span className="pill">{view.edges.toLocaleString()} edges</span>
            <span className="pill dim" data-testid="rebuilt">
              {rebuilt
                ? `${WRITER} rebuilt it ${ago(rebuilt.at)}: ${signed(rebuilt.nodes_added)} nodes, ${signed(rebuilt.edges_added)} edges in ${rebuilt.seconds}s`
                : "Not built yet"}
            </span>
          </div>
        )}
      </header>
      {view?.failed && (
        <p role="alert" data-testid="failed">
          {WRITER}'s last rebuild failed {ago(view.failed.at)}: {view.failed.error}. The previous
          graph still answers; the next new note retries.
        </p>
      )}
      {error && <p role="alert">{error}</p>}

      <div className="graph-layout">
        <section aria-label="Query">
          <form className="ask" onSubmit={submit}>
            <label htmlFor="question">Ask the graph</label>
            <div className="ask-row">
              <input
                id="question"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder='e.g. "what keeps viewers past three seconds?"'
                maxLength={500}
              />
              <button type="submit" className="primary" disabled={asking || !question.trim()}>
                {asking ? "Asking..." : "Ask"}
              </button>
            </div>
          </form>
          {answer && (
            <figure className="answer" data-testid="answer">
              <figcaption className="faint">{answer.question}</figcaption>
              <pre>{answer.text}</pre>
            </figure>
          )}
          {empty && (
            <div className="empty" data-testid="graph-empty">
              <p>
                The graph grows from the notes and artifacts agents write. {WRITER} folds them in
                about 30 seconds after the last one lands.
              </p>
              <ul className="faint">
                <li>Start a run of a workflow; every note it writes becomes memory.</li>
                <li>Drop a markdown brief into the world folder to seed what it knows.</li>
              </ul>
              <Link to="/worlds/$worldId" params={{ worldId }} className="button">
                Start a run
              </Link>
            </div>
          )}
        </section>
        <aside aria-label="God nodes" className="card">
          <h2>God nodes</h2>
          <p className="faint">What this world's knowledge revolves around. Pick one to ask about it.</p>
          {view && view.god_nodes.length > 0 ? (
            <ul className="gods" data-testid="god-nodes">
              {view.god_nodes.map((g) => (
                <li key={g.label}>
                  <button
                    type="button"
                    className="link"
                    onClick={() => {
                      const q = `What do we know about ${g.label}?`;
                      setQuestion(q);
                      void ask(q);
                    }}
                  >
                    {g.label}
                  </button>
                  <span className="faint">{g.degree} links</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="faint">None yet.</p>
          )}
        </aside>
      </div>
    </main>
  );
}
