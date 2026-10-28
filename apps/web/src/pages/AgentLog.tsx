import type { Agent, LogEntry } from "../api/events";
import { who } from "../api/tasks";

/** Tool names as the agent sees them, without the in-process server prefix. */
const short = (tool: string | null | undefined) => (tool ?? "").replace(/^mcp__swarmify__/, "");

export const usd = (n: number) => `$${n.toFixed(4)}`;

function line(e: LogEntry): string {
  const name = who(e.agent);
  switch (e.kind) {
    case "tool":
      return `${name} used ${short(e.tool)}`;
    case "denied":
      return `${name} was refused ${short(e.tool)}: ${e.detail}`;
    default:
      return `${name}: ${e.detail}`;
  }
}

/** The agents of one swarm stage and its live log of every tool call the hooks saw. */
export function AgentLog({ agents, log }: { agents: Agent[]; log: LogEntry[] }) {
  return (
    <section className="agents" aria-label="Agents" data-testid="agents">
      <h2>Agents</h2>
      {agents.length === 0 ? (
        <p className="faint">No agent has started yet. One starts when this stage runs.</p>
      ) : (
        <ul className="agent-list">
          {agents.map((a) => (
            <li key={a.id} data-testid="agent">
              <b>{who(a.name)}</b> <span className={`status status-${a.status}`}>{a.status}</span>
              <span className="faint">
                {" "}
                {a.model ? `· on ${a.model} ` : ""}· estimated {usd(a.cost_usd)} · skills: {(a.skills ?? []).join(", ") || "none"}
              </span>
              {a.session_id && (
                <span className="faint" title={a.session_id}>
                  {" "}
                  · session <code>{a.session_id.slice(-6)}</code>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      <h3>Live log</h3>
      {log.length === 0 ? (
        <p className="faint">Nothing yet. Every tool call appears here as it happens.</p>
      ) : (
        <ol className="log" data-testid="log">
          {log.map((e) => (
            <li key={e.seq} className={`log-${e.kind}`}>
              <time className="faint" dateTime={e.ts}>
                {e.ts.slice(11, 19)}
              </time>{" "}
              {line(e)}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
