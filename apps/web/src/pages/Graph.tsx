import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { graphRoute } from "../router";
import { cap, the } from "../skin";
import { FileLink } from "./Artifacts";

type World = components["schemas"]["World"];
type View = components["schemas"]["GraphView"];
type Node = components["schemas"]["GraphNode"];


function ago(ts: string, now = Date.now()): string {
  const s = Math.max(0, Math.round((now - new Date(ts).getTime()) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ts).toLocaleDateString();
}

const signed = (n: number) => (n >= 0 ? `+${n}` : `${n}`);
const usd = (n: number) => `$${n.toFixed(2)}`;
const took = (s: number) => (s < 90 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`);

/** Where a node came from: its run's timeline and the note itself. */
function Sources({ node, worldId }: { node: Node; worldId: string }) {
  return (
    <>
      {node.run_id && (
        <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: node.run_id }} className="faint">
          run
        </Link>
      )}{" "}
      {node.source_file && (
        <FileLink a={{ path: node.source_file, name: node.source_file }} worldId={worldId} className="faint">
          note
        </FileLink>
      )}
    </>
  );
}

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

  async function acceptShrink() {
    setError(null);
    const { error } = await api.POST("/api/worlds/{world_id}/graph/accept-shrink", {
      params: { path: { world_id: worldId } },
    });
    if (error) setError("Could not accept the shrink. Try again in a moment.");
    void load().catch(() => undefined);
  }

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

  function askAbout(label: string) {
    const q = `What do we know about ${label}?`;
    setQuestion(q);
    void ask(q);
  }

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    void ask(question);
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
        <h1>No such world</h1>
        <p className="sub">
          It may have been deleted. <Link to="/">Back to your worlds.</Link>
        </p>
      </main>
    );
  }

  const rebuilt = view?.rebuilt;
  const empty = view !== null && view.nodes === 0;
  const top = Math.max(1, ...(view?.god_nodes ?? []).map((g) => g.degree));
  return (
    <main>
      <header className="page">
        <div>
          <h1>Memory</h1>
          <p className="sub" data-testid="rebuilt">
            {rebuilt
              ? `${cap(the("writer"))} rebuilt it ${ago(rebuilt.at)}: ${signed(rebuilt.nodes_added)} nodes, ${signed(rebuilt.edges_added)} edges in ${rebuilt.seconds}s, est. ${usd(rebuilt.cost_usd)}`
              : view
                ? "Not built yet"
                : "Everything this world has learned, as one graph."}
          </p>
        </div>
      </header>
      {view?.failed && !view.failed.shrink && (
        <p role="alert" data-testid="failed">
          {cap(the("writer"))}'s last rebuild failed {ago(view.failed.at)}: {view.failed.error}. The previous
          graph still answers; the next new note retries.
        </p>
      )}
      {view?.failed?.shrink && (
        <div role="alert" data-testid="failed">
          <p>
            {cap(the("writer"))}'s last rebuild failed {ago(view.failed.at)}: {view.failed.error}. graphify
            refused to write a smaller graph, and keeps refusing until you accept the shrink. The
            previous graph still answers.
          </p>
          {view.accepted ? (
            <p role="status">Rebuilding with the shrink accepted...</p>
          ) : (
            <button type="button" className="primary" onClick={() => void acceptShrink()}>
              Accept the shrink
            </button>
          )}
        </div>
      )}
      {error && <p role="alert">{error}</p>}

      {view && (
        <dl className="kpis" data-testid="graph-size">
          <div className="kpi">
            <dt>Nodes</dt>
            <dd className="num">{view.nodes.toLocaleString()}</dd>
          </div>
          <div className="kpi">
            <dt>Edges</dt>
            <dd className="num">{view.edges.toLocaleString()}</dd>
          </div>
          <div className="kpi">
            <dt>Rebuilds</dt>
            <dd className="num">{view.spent.rebuilds}</dd>
          </div>
          <div className="kpi" data-testid="spent" title={`${took(view.spent.seconds)} of extraction`}>
            <dt>Spent on memory</dt>
            <dd className="num">{usd(view.spent.cost_usd)}</dd>
          </div>
        </dl>
      )}

      <section className="panel" aria-label="Query">
        <form className="ask-row" onSubmit={submit}>
          <input
            aria-label="Ask the graph"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="What keeps viewers past three seconds?"
            maxLength={500}
          />
          <button type="submit" disabled={asking || !question.trim()}>
            {asking ? "Asking..." : "Ask"}
          </button>
        </form>
        {answer && (
          <figure className="answer" data-testid="answer">
            <figcaption className="faint">{answer.question}</figcaption>
            <pre>{answer.text}</pre>
          </figure>
        )}
      </section>

      {empty && (
        <div className="empty" data-testid="graph-empty">
          <p>
            The graph grows from the notes and artifacts agents write. {cap(the("writer"))} folds them in
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
      {view && !empty && (
        <div className="mem-cols">
          <section className="panel" aria-labelledby="gods">
            <div className="panel-head">
              <h2 id="gods">Most connected notes</h2>
              <span className="faint">Links</span>
            </div>
            <ol className="mem-rows" data-testid="god-nodes">
              {view.god_nodes.map((g) => (
                <li key={g.label} className="bar-row">
                  <span className="bar-label">
                    <button type="button" className="link" onClick={() => askAbout(g.label)}>
                      {g.label}
                    </button>
                    <span className="thin-bar" style={{ width: `${(g.degree / top) * 100}%` }} />
                  </span>
                  <span className="num">{g.degree}</span>
                </li>
              ))}
            </ol>
            {view.god_nodes.length === 0 && <p className="faint">None yet.</p>}
          </section>
          <div className="mem-side">
            <section className="panel" aria-labelledby="communities">
              <div className="panel-head">
                <h2 id="communities">Communities</h2>
                <span className="faint">Nodes</span>
              </div>
              {view.communities.length > 0 ? (
                <ul className="mem-rows" data-testid="communities">
                  {view.communities.map((c) => (
                    <li key={c.id}>
                      <span className="mem-main">
                        <b>{c.name}</b>
                        <span className="faint">
                          {c.nodes.map((n, i) => (
                            <span key={`${n.label} ${n.source_file}`}>
                              {i > 0 && ", "}
                              <button type="button" className="link" onClick={() => askAbout(n.label)}>
                                {n.label}
                              </button>{" "}
                              <Sources node={n} worldId={worldId} />
                            </span>
                          ))}
                        </span>
                      </span>
                      <span className="num">{c.size}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="faint" data-testid="no-communities">
                  No communities yet. {cap(the("writer"))} finds them on the next rebuild.
                </p>
              )}
            </section>
            {view.surprises.length > 0 && (
              <section className="panel" aria-labelledby="surprises">
                <div className="panel-head">
                  <h2 id="surprises">Surprising links</h2>
                  <span className="faint num">{view.surprises.length}</span>
                </div>
                <ul className="mem-rows" data-testid="surprises">
                  {view.surprises.map((x) => (
                    <li key={`${x.source.label} ${x.relation} ${x.target.label}`}>
                      <span className="mem-main">
                        <span>
                          {x.source.label} <span className="faint">{(x.relation || "links to").replace(/_/g, " ")}</span>{" "}
                          {x.target.label}
                        </span>
                        <span className="faint">
                          {x.why && `${x.why} `}
                          <Sources node={x.source} worldId={worldId} /> <Sources node={x.target} worldId={worldId} />
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
