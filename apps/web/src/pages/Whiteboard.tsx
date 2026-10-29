import { type FormEvent, useState } from "react";

import { api } from "../api/client";
import type { Run } from "../api/runs";
import { type Task, who } from "../api/tasks";

type Stage = Run["stages"][number];

const COLUMNS = [
  { status: "open", title: "Open", empty: "Nothing waiting. Add a task to steer the swarm." },
  { status: "claimed", title: "Claimed", empty: "Nothing in flight." },
  { status: "done", title: "Done", empty: "Nothing finished yet." },
] as const;
const ENDED = ["done", "failed", "skipped"];

/** The whiteboard of one swarm stage: its tasks by state, and a form to add one by hand. */
export function Whiteboard(props: {
  runId: string;
  stage: Stage;
  tasks: Task[];
  onAdded: (task: Task) => void;
}) {
  const { runId, stage, tasks, onAdded } = props;
  const [title, setTitle] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ended = ENDED.includes(stage.status);
  const done = new Set(tasks.filter((t) => t.status === "done").map((t) => t.id));
  const count = (status: Task["status"]) => tasks.filter((t) => t.status === status).length;

  async function add(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const { data, error } = await api.POST("/api/runs/{run_id}/stages/{stage_id}/tasks", {
        params: { path: { run_id: runId, stage_id: stage.id } },
        body: { title, body: "", depends_on: [], added_by: "operator" },
      });
      if (!data) {
        const detail = (error as { detail?: unknown } | undefined)?.detail;
        throw new Error(typeof detail === "string" ? detail : "The task was not added.");
      }
      onAdded(data);
      setTitle("");
    } catch (err) {
      setError(err instanceof TypeError ? "Could not reach the API." : (err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="whiteboard" aria-label={`Whiteboard of ${stage.name}`} data-testid="whiteboard">
      <div className="row">
        <div>
          <h3>Whiteboard</h3>
          <p className="faint" data-testid="whiteboard-summary">
            {tasks.length === 0
              ? "No tasks yet."
              : `${count("open")} open, ${count("claimed")} claimed, ${count("done")} done.`}
          </p>
        </div>
        {ended ? (
          <p className="faint">This stage has ended, so its whiteboard is read-only.</p>
        ) : (
          <form className="add-task" onSubmit={add}>
            <label htmlFor="new-task" className="visually-hidden">
              New task
            </label>
            <input
              id="new-task"
              value={title}
              maxLength={300}
              placeholder="What should the swarm look at next?"
              onChange={(e) => setTitle(e.target.value)}
            />
            <button type="submit" className="primary" disabled={saving || !title.trim()}>
              {saving ? "Adding..." : "Add task"}
            </button>
          </form>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
      <div className="board">
        {COLUMNS.map((col) => {
          const items = tasks.filter((t) => t.status === col.status);
          return (
            <section key={col.status} className="col" data-testid={`col-${col.status}`}>
              <h3>
                {col.title} <span>{items.length}</span>
              </h3>
              {items.length === 0 ? (
                <p className="col-empty">{ended && col.status === "open" ? "Nothing waiting." : col.empty}</p>
              ) : (
                <ul>
                  {items.map((t) => (
                    <TaskCard key={t.id} task={t} waitingOn={(t.depends_on ?? []).filter((d) => !done.has(d)).length} />
                  ))}
                </ul>
              )}
            </section>
          );
        })}
      </div>
    </section>
  );
}

function TaskCard({ task, waitingOn }: { task: Task; waitingOn: number }) {
  const meta =
    task.status === "open"
      ? `added by ${who(task.added_by)}${waitingOn ? ` · waiting on ${waitingOn} ${waitingOn === 1 ? "task" : "tasks"}` : ""}`
      : `${task.status === "done" ? "done" : "claimed"} by ${who(task.owner)}`;
  return (
    <li className={`task task-${task.status}`}>
      <b>{task.title}</b>
      <span className="faint">{meta}</span>
      {(task.notes ?? []).length > 0 && (
        <ul className="notes">
          {(task.notes ?? []).map((n, i) => (
            <li key={i}>
              <span className="faint">{who(n.author)}:</span> {n.body}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
