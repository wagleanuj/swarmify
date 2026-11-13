import { useSyncExternalStore } from "react";

import { onRefresh } from "../refresh";
import { api } from "./client";
import { seqOf } from "./events";
import type { Gate } from "./runs";

/** Every world's open gates: `null` until the first load. */
type Open = { gates: Gate[] | null; failed: boolean };

// One GET /api/gates and one all-worlds stream, shared by the sidebar's count and the pages
// that list gates, so a page does not hold a second stream (the browser allows six per host).
// The stream opens after the first load, from the position its snapshot reflects: no run.updated
// between the two is missed, and history is not replayed (it refetched once per past update).
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
    .then(({ data, error, response }) => {
      if (error || !data) throw new Error("load failed");
      if (asked !== generation) return;
      publish({ gates: data, failed: false });
      follow(seqOf(response) ?? "now");
    })
    .catch(() => {
      if (asked !== generation) return;
      publish({ ...open, failed: true });
      follow("now"); // its next run.updated retries the load
    });
}

function follow(since: number | "now") {
  if (source) return;
  source = new EventSource(`/api/events?since=${since}`);
  // One refetch per burst of run updates, not one per event.
  source.addEventListener("run.updated", () => {
    clearTimeout(timer);
    timer = window.setTimeout(reloadGates, 100);
  });
}

/** Drop an answered gate at once; the run's next run.updated refetches the list. Refetching
 * here could beat the workflow to the message and show the answered gate again. */
export function dropGate(stageId: string) {
  if (open.gates) publish({ ...open, gates: open.gates.filter((g) => g.stage_id !== stageId) });
}

// A refresh (refresh.ts) reads the gates again and reopens the stream from that snapshot: on a
// phone the stream may have died in the background without a word.
function resync() {
  source?.close();
  source = null;
  generation += 1;
  reloadGates();
}
let stopResync: (() => void) | undefined;

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    reloadGates();
    stopResync = onRefresh(resync);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size > 0) return;
    stopResync?.();
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

/** The one Needs-you count every label reads (the tab badge, a world's overview, the Office HUD
 * and tab, the world filter): the open gates, in one world or all. `null` until the gates load,
 * so no label says 0 or "nothing waits" before it knows. */
export const needsYou = (gates: Gate[] | null, worldId?: string): number | null =>
  gates === null ? null : worldId ? gates.filter((g) => g.world_id === worldId).length : gates.length;
