import type { Task } from "./events";
import { role } from "../skin";

export type { Task };

/** Replace or add `task`, ignoring a snapshot older than the one held. Keeps creation order. */
export function upsert(tasks: Task[], task: Task): Task[] {
  const held = tasks.find((t) => t.id === task.id);
  if (!held) return [...tasks, task];
  if (held.updated_at > task.updated_at) return tasks;
  return tasks.map((t) => (t.id === task.id ? task : t));
}

const OTHERS: Record<string, string> = { operator: "you", swarmify: "the swarm" };

/** Who did something, as the skin names them (ADR 0004): code says worker-1, the page Worker 1. */
export const who = (name: string | null) => (name ? (OTHERS[name] ?? role(name)) : "nobody");
