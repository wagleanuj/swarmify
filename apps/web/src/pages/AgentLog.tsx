import type { Agent, LogEntry } from "../api/events";
import { type Task, who } from "../api/tasks";

export const usd = (n: number) => `$${n.toFixed(4)}`;

/** Tool names as the agent sees them, without the in-process server prefix. */
const short = (tool: string | null | undefined) => (tool ?? "").replace(/^mcp__swarmify__/, "");

/** A path as a person reads it: inside a build folder from its root, else its last two parts. */
function path(p: unknown): string {
  const s = String(p ?? "");
  const build = s.split("/build/");
  if (build.length > 1) return build.at(-1)!;
  return s.split("/").slice(-2).join("/");
}

/** A note title as the file the swarm writes it to, `notes/<slug>.md`. */
const slug = (title: unknown) =>
  `notes/${String(title ?? "note").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}.md`;

/** What one Bash command did, in words. */
function command(cmd: string): string {
  if (/\bnpm (install|i|ci)\b/.test(cmd)) return "Installed packages";
  if (/\btsc\b/.test(cmd)) return "Checked that it compiles";
  if (/\bcurl\b|\bwget\b/.test(cmd)) return "Downloaded files";
  if (/^\s*(ls|cat|head|tail|find)\b/.test(cmd)) return "Looked through the files";
  return "Ran a command";
}

/** One tool call in plain words. `tasks` names the whiteboard tasks a call points at. */
function action(e: LogEntry, tasks: Map<string, string>): string {
  const input = (e.input ?? {}) as Record<string, unknown>;
  const task = tasks.get(String(input.task_id ?? "")) ?? "the next task";
  switch (short(e.tool)) {
    case "WebSearch":
      return input.query ? `Searching the web for "${String(input.query)}"` : "Searching the web";
    case "WebFetch":
      try {
        return `Reading ${new URL(String(input.url)).host}`;
      } catch {
        return "Reading a web page";
      }
    case "Read":
      return `Reading ${path(input.file_path)}`;
    case "Write":
      return `Wrote ${path(input.file_path)}`;
    case "Edit":
    case "MultiEdit":
      return `Edited ${path(input.file_path)}`;
    case "Glob":
    case "Grep":
      return "Searched the files";
    case "Bash":
      return command(String(input.command ?? ""));
    case "query_graph":
      return "Searching the world's memory";
    case "list_tasks":
      return "Reading the whiteboard";
    case "add_task":
      return `Added a task: ${String(input.title ?? "")}`;
    case "claim_task":
      return `Claimed: ${task}`;
    case "finish_task":
      return `Finished: ${task}`;
    case "write_note":
      return `Writing ${slug(input.title)}`;
    case "read_note":
      return "Reading the notes";
    case "write_artifact":
      return `Writing artifacts/${String(input.name ?? "")}`;
    default:
      return `Used ${short(e.tool)}`;
  }
}

/** A log entry's detail with any JSON payload in it pulled out, and that payload in one line. */
export function payload(detail: string): { text: string; json?: string; summary?: string } {
  const at = detail.search(/[[{]/);
  if (at < 0) return { text: detail };
  const raw = detail.slice(at);
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    return { text: detail };
  }
  return { text: detail.slice(0, at).trim(), json: JSON.stringify(value, null, 2), summary: summarise(value) };
}

/** "Planned 5 tasks", "Passed, 2 items", "3 items": what a JSON answer amounts to. */
function summarise(value: unknown): string {
  const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
  if (Array.isArray(value)) return count(value.length, "item");
  if (value && typeof value === "object") {
    const o = value as Record<string, unknown>;
    if (Array.isArray(o.tasks)) return `Planned ${count(o.tasks.length, "task")}`;
    if (typeof o.passed === "boolean") {
      const items = Array.isArray(o.items) ? `, ${count(o.items.length, "item")}` : "";
      return `${o.passed ? "Passed" : "Failed"}${items}`;
    }
    if (Array.isArray(o.items)) return count(o.items.length, "item");
    if ("skill" in o) return o.skill ? "Proposed a skill change" : "Proposed no change";
    return `An answer with ${Object.keys(o).join(", ") || "nothing"}`;
  }
  return String(value);
}

/** One log entry in words, without who did it. */
export function describe(e: LogEntry, tasks: Map<string, string> = new Map()): string {
  switch (e.kind) {
    case "tool":
      return action(e, tasks);
    case "denied":
      // A connector's own process has no tool: its line says what the sandbox blocked.
      return e.tool ? `Refused ${short(e.tool)}: ${e.detail}` : e.detail;
    case "session": {
      const model = /started on (\S+)/.exec(e.detail)?.[1];
      return model ? `Started on ${model}` : "Started";
    }
    default:
      return payload(e.detail).text || e.detail;
  }
}

const titles = (tasks: Task[]) => new Map(tasks.map((t) => [t.id, t.title]));

/** What an agent is doing now: its latest log entry, in words. */
function doing(agent: Agent, log: LogEntry[], tasks: Map<string, string>): string {
  if (agent.status === "done") return "Finished";
  if (agent.status === "failed") return "Stopped";
  const last = log.filter((e) => e.agent === agent.name).at(-1);
  return last ? describe(last, tasks) : "Starting";
}

/** The agents of one stage as compact cards: who, role, state, cost and what each does now. */
export function AgentCards({ agents, log, tasks }: { agents: Agent[]; log: LogEntry[]; tasks: Task[] }) {
  const named = titles(tasks);
  return (
    <section className="agents" aria-label="Agents" data-testid="agents">
      <h3>Agents</h3>
      {agents.length === 0 ? (
        <p className="faint">No agent has started yet. One starts when this stage runs.</p>
      ) : (
        <ul className="agent-cards">
          {agents.map((a) => {
            const skills = a.skills ?? [];
            return (
              <li key={a.id} className="agent-card" data-testid="agent">
                <div className="row">
                  <b>{who(a.name)}</b>
                  <span className={`status status-${a.status}`}>{a.status}</span>
                </div>
                <span className="faint">
                  {a.role} · {usd(a.cost_usd)}
                  {a.model ? ` · ${a.model}` : ""}
                </span>
                <span className="doing" data-testid="doing">
                  {doing(a, log, named)}
                </span>
                {skills.length > 0 && (
                  <details className="agent-skills">
                    <summary>
                      {skills.length} {skills.length === 1 ? "skill" : "skills"}
                    </summary>
                    <p className="faint">{skills.join(", ")}</p>
                  </details>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** The selected stage's live log, in words. A JSON answer collapses to one line that opens. */
export function LiveLog({ log, tasks = [], title = "Live log" }: { log: LogEntry[]; tasks?: Task[]; title?: string }) {
  const named = titles(tasks);
  return (
    <section className="live-log" aria-label={title}>
      <h3>{title}</h3>
      {log.length === 0 ? (
        <p className="faint">Nothing yet. Every step an agent takes appears here as it happens.</p>
      ) : (
        <ol className="log" data-testid="log">
          {log.map((e) => {
            const json = e.kind === "result" ? payload(e.detail) : null;
            return (
              <li key={e.seq} className={`log-${e.kind}`}>
                <time className="faint" dateTime={e.ts}>
                  {e.ts.slice(11, 19)}
                </time>{" "}
                <span>
                  {who(e.agent)}: {describe(e, named)}
                </span>
                {json?.json && (
                  <details className="payload">
                    <summary>{json.summary}</summary>
                    <pre>{json.json}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
