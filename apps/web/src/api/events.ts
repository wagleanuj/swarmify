import { useCallback, useEffect, useRef, useState } from "react";

import { useRefresh } from "../refresh";
import type { components } from "./schema";

export type Run = components["schemas"]["Run"];
export type Task = components["schemas"]["Task"];
export type Call = components["schemas"]["Call"];
export type Agent = components["schemas"]["Agent"];
export type LogEntry = components["schemas"]["LogEntry"];
export type Verdict = components["schemas"]["Verdict"];
export type ArtifactFile = components["schemas"]["ArtifactFile"];
/** One metric in words: a count as "9,000 views", retention (a `_percent` or `_seconds`
 * metric) as "avg viewed 62.5%" or "avg view 12.0 s". */
function metricWords(metric: string, n: number): string {
  const name = (suffix: string) => metric.slice(0, -suffix.length).replaceAll("_", " ");
  if (metric.endsWith("_percent")) return `${name("_percent")} ${n.toFixed(1)}%`;
  if (metric.endsWith("_seconds")) return `${name("_seconds")} ${n.toFixed(1)} s`;
  return `${n.toLocaleString("en")} ${metric.replaceAll("_", " ")}`;
}

/** A publish record's Outcome as "9,000 views, 12 clicks", numbers by metric as its connectors
 * reported them; null before any. A record from before outcomes were generic kept `views`. */
export function outcomeLine(metrics: unknown): string | null {
  const shown = Object.entries((metrics ?? {}) as Record<string, unknown>).filter(
    (e): e is [string, number] => typeof e[1] === "number",
  );
  return shown.length ? shown.map(([m, n]) => metricWords(m, n)).join(", ") : null;
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
  /** Only the settings that changed, with the world's id. */
  "world.updated": Partial<components["schemas"]["World"]>;
  /** A workflow was saved; reload the list to see it. */
  "workflow.saved": unknown;
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
  "world.updated",
  "workflow.saved",
];

/** The event-log position a snapshot response reflects: the API stamps every GET with it. Null
 * for a response that did not come from the API, such as a proxy's error page. */
export function seqOf(response: Response): number | null {
  const seq = response.headers.get("X-Event-Seq");
  return seq === null ? null : Number(seq);
}

/** Follow one world's event stream with a single EventSource (ADR 0002), from the snapshot the
 * page loaded. Pass each snapshot response the handlers patch to the returned `saw`: the stream
 * opens at the oldest of their positions, so an event landing between a GET and the subscribe
 * still arrives and the world's history is never replayed. Reconnects resume by Last-Event-ID.
 * On a refresh (refresh.ts) `saw` is a new function, so the page's loading effects run again,
 * and the stream reopens from the new snapshots' position. */
export function useWorldEvents(worldId: string, handlers: Handlers): (snapshot: Response) => void {
  const latest = useRef(handlers);
  latest.current = handlers;
  const tick = useRefresh();
  const [since, setSince] = useState<{ tick: number; seq: number } | null>(null);
  const saw = useCallback(
    (snapshot: Response) => {
      const seq = seqOf(snapshot);
      if (seq !== null)
        setSince((prev) => (prev === null || prev.tick !== tick ? { tick, seq } : { tick, seq: Math.min(prev.seq, seq) }));
    },
    [tick],
  );
  // The stream belongs to this refresh's snapshots: a refresh closes it until they answer.
  const at = since?.tick === tick ? since.seq : null;
  useEffect(() => {
    if (at === null) return;
    let source: EventSource;
    let retry: number | undefined;
    let last = at;
    const open = (from: number) => {
      source = new EventSource(`/api/worlds/${worldId}/events?since=${from}`);
      for (const type of TYPES) {
        source.addEventListener(type, (e) => {
          const message = e as MessageEvent<string>;
          if (message.lastEventId) last = Number(message.lastEventId);
          const event = JSON.parse(message.data) as { payload: never };
          latest.current[type]?.(event.payload);
        });
      }
      // The browser gives up for good on a non-stream answer (a proxy's 502 while the API
      // restarts): reopen from the last event seen, so the page does not go quietly stale.
      source.addEventListener("error", () => {
        if (source.readyState !== EventSource.CLOSED) return;
        retry = window.setTimeout(() => open(last), 5000);
      });
    };
    open(at);
    return () => {
      clearTimeout(retry);
      source.close();
    };
  }, [worldId, at, tick]);
  return saw;
}
