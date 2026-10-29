import { useSyncExternalStore } from "react";

import { api } from "./client";
import type { Gate } from "./runs";

/** Every world's open gates: `null` until the first load. */
type Open = { gates: Gate[] | null; failed: boolean };

// One GET /api/gates and one all-worlds stream, shared by the sidebar's count and the pages
// that list gates, so a page does not hold a second stream (the browser allows six per host).
// `since=now` skips history: replaying it refetched once per past run.updated.
const EMPTY: Open = { gates: null, failed: false };
let open = EMPTY;
let generation = 0;
const listeners = new Set<() => void>();
let source: EventSource | null = null;
let timer: number | undefined;

function publish(next: Open) {
  open = next;
  for (const listener of listeners) listener();
}

/** Refetch the open gates; every subscriber sees the result. */
export function reloadGates() {
  const asked = generation;
  api
    .GET("/api/gates")
    .then(({ data, error }) => {
      if (error || !data) throw new Error("load failed");
      if (asked === generation) publish({ gates: data, failed: false });
    })
    .catch(() => {
      if (asked === generation) publish({ ...open, failed: true });
    });
}

/** Drop an answered gate at once; the run's next run.updated refetches the list. Refetching
 * here could beat the workflow to the message and show the answered gate again. */
export function dropGate(stageId: string) {
  if (open.gates) publish({ ...open, gates: open.gates.filter((g) => g.stage_id !== stageId) });
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    reloadGates();
    source = new EventSource("/api/events?since=now");
    // One refetch per burst of run updates, not one per event.
    source.addEventListener("run.updated", () => {
      clearTimeout(timer);
      timer = window.setTimeout(reloadGates, 100);
    });
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size > 0) return;
    clearTimeout(timer);
    source?.close();
    source = null;
    generation += 1;
    open = EMPTY;
  };
}

export function useOpenGates(): Open {
  return useSyncExternalStore(subscribe, () => open);
}
