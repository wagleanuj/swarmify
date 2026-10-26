import type { Task } from "./events";

export type { Task };

/** Replace or add `task`, ignoring a snapshot older than the one held. Keeps creation order. */
export function upsert(tasks: Task[], task: Task): Task[] {
  const held = tasks.find((t) => t.id === task.id);
  if (!held) return [...tasks, task];
  if (held.updated_at > task.updated_at) return tasks;
  return tasks.map((t) => (t.id === task.id ? task : t));
}

/** Pied Piper names belong to the cast; the person at the keyboard is "you". */
export const who = (name: string | null) => (name === "operator" ? "you" : (name ?? "nobody"));
