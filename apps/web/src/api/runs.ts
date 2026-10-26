import { useEffect, useRef } from "react";

import type { components } from "./schema";

export type Run = components["schemas"]["Run"];
export type Call = components["schemas"]["Call"];

/** Call `onRun` with every run snapshot the world's event stream carries (ADR 0002), and
 * `onCall` with every connector call snapshot, progress included. One stream per page. */
export function useRunUpdates(
  worldId: string,
  onRun: (run: Run) => void,
  onCall?: (call: Call) => void,
) {
  const latest = useRef({ onRun, onCall });
  latest.current = { onRun, onCall };
  useEffect(() => {
    const source = new EventSource(`/api/worlds/${worldId}/events`);
    const payload = (e: Event) => (JSON.parse((e as MessageEvent<string>).data) as { payload: never }).payload;
    source.addEventListener("run.updated", (e) => latest.current.onRun(payload(e)));
    source.addEventListener("connector_call.updated", (e) => latest.current.onCall?.(payload(e)));
    return () => source.close();
  }, [worldId]);
}

export const STATUS_LABEL: Record<Run["status"], string> = {
  queued: "queued",
  running: "running",
  waiting_gate: "needs you",
  paused: "paused",
  done: "done",
  failed: "failed",
  aborted: "aborted",
};

export function progress(run: Run): string {
  const done = run.stages.filter((s) => s.status === "done").length;
  return `${done} of ${run.stages.length} stages`;
}

/** One confident sentence about where the run is. */
export function statusLine(run: Run): string {
  const at = run.stages.find((s) => s.status === "running" || s.status === "waiting");
  const failed = run.stages.find((s) => s.status === "failed");
  const total = run.stages.length;
  switch (run.status) {
    case "queued":
      return "Queued. It starts as soon as one of this world's two run slots is free.";
    case "running":
      return at ? `Running stage ${at.index + 1} of ${total}: ${at.name}.` : "Running.";
    case "waiting_gate":
      return at ? `Waiting for you at stage ${at.index + 1}: ${at.name}.` : "Waiting for you.";
    case "paused":
      return "Paused. Nothing moves until it is resumed.";
    case "done":
      return `Done. All ${total} stages finished.`;
    case "failed":
      return failed
        ? `Failed at stage ${failed.index + 1}, ${failed.name}: ${failed.error ?? "no error recorded"}.`
        : "Failed.";
    case "aborted":
      return "Aborted. Finished stages keep their artifacts.";
  }
}
