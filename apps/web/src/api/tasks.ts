import type { Task } from "./events";

export type { Task };

/** Replace or add `task`, ignoring a snapshot older than the one held. Keeps creation order. */
export function upsert(tasks: Task[], task: Task): Task[] {
  const held = tasks.find((t) => t.id === task.id);
  if (!held) return [...tasks, task];
  if (held.updated_at > task.updated_at) return tasks;
  return tasks.map((t) => (t.id === task.id ? task : t));
}

/** Pied Piper names belong to the skin (ADR 0004): code says worker-1, the page says Gilfoyle. */
const CAST: Record<string, string> = {
  operator: "you",
  swarmify: "the swarm",
  "worker-1": "Gilfoyle",
  "worker-2": "Dinesh",
  "worker-3": "Jared",
  "worker-4": "Big Head",
  planner: "Richard",
  judge: "Erlich",
};

export const who = (name: string | null) => (name ? (CAST[name] ?? name) : "nobody");
