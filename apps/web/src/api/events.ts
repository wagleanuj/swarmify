import { useEffect, useRef } from "react";

import type { components } from "./schema";

export type Run = components["schemas"]["Run"];
export type Task = components["schemas"]["Task"];
export type Call = components["schemas"]["Call"];
export type Agent = components["schemas"]["Agent"];
export type LogEntry = components["schemas"]["LogEntry"];
export type Verdict = components["schemas"]["Verdict"];
export type ArtifactFile = components["schemas"]["ArtifactFile"];
/** A publish record's Outcome as "9,000 views, 12 clicks", numbers by metric as its connectors
 * reported them; null before any. A record from before outcomes were generic kept `views`. */
export function outcomeLine(metrics: unknown): string | null {
  const shown = Object.entries((metrics ?? {}) as Record<string, unknown>).filter(
    (e): e is [string, number] => typeof e[1] === "number",
  );
  return shown.length ? shown.map(([m, n]) => `${n.toLocaleString("en")} ${m.replaceAll("_", " ")}`).join(", ") : null;
}

/** The Outcome of a publish record's fields, with the day it was read. */
export function outcomeOf(fields: Record<string, unknown>): { line: string; day: string } | null {
  const line = outcomeLine(fields.outcomes ?? { views: fields.views });
  return line ? { line, day: String(fields.outcomes_at ?? fields.views_at ?? "").slice(0, 10) } : null;
}

/** What `artifact.created` carries: enough to know which stage to reload. */
export type ArtifactCreated = { type: string; path: string; author: string; stage_id: string };

/** The world stream's event types a page can follow, each with the snapshot it carries. */
export type WorldEvents = {
  "run.updated": Run;
  "task.updated": Task;
  "connector_call.updated": Call;
  "agent.updated": Agent;
  "agent.logged": LogEntry;
  "verdict.created": Verdict;
  "artifact.created": ArtifactCreated;
};
type Handlers = { [K in keyof WorldEvents]?: (payload: WorldEvents[K]) => void };
const TYPES: (keyof WorldEvents)[] = [
  "run.updated",
  "task.updated",
  "connector_call.updated",
  "agent.updated",
  "agent.logged",
  "verdict.created",
  "artifact.created",
];

/** Follow one world's event stream with a single EventSource (ADR 0002). */
export function useWorldEvents(worldId: string, handlers: Handlers) {
  const latest = useRef(handlers);
  latest.current = handlers;
  useEffect(() => {
    const source = new EventSource(`/api/worlds/${worldId}/events`);
    for (const type of TYPES) {
      source.addEventListener(type, (e) => {
        const event = JSON.parse((e as MessageEvent<string>).data) as { payload: never };
        latest.current[type]?.(event.payload);
      });
    }
    return () => source.close();
  }, [worldId]);
}
