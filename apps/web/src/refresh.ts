/** Refreshing the open page in place, never by reloading the document: a pull on a phone and
 * coming back to the app both bump one counter. Every `useWorldEvents` hands its page a new `saw`
 * on a bump, so the page's loading effects (which depend on `saw`) fetch again and the event stream
 * reopens from the new snapshot's X-Event-Seq; a page without a stream reads `useRefresh()`. */
import { useSyncExternalStore } from "react";

import { requestsInFlight, watchRequests } from "./api/client";

let tick = 0;
const listeners = new Set<() => void>();
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};

/** The refresh count: a loading effect that lists it runs again on every refresh. */
export const useRefresh = () => useSyncExternalStore(subscribe, () => tick);
/** For stores outside React (the open gates): called on every refresh. */
export const onRefresh = subscribe;

/** Refetch the open page and reopen its streams. Resolves once the requests it started settle. */
export function refresh(): Promise<void> {
  tick += 1;
  listeners.forEach((l) => l());
  return new Promise((done) => {
    // The effects that fetch run after React commits; give them a frame to start.
    setTimeout(() => {
      if (requestsInFlight() === 0) return done();
      const stop = watchRequests(() => {
        if (requestsInFlight() > 0) return;
        stop();
        done();
      });
    }, 50);
  });
}

/** Away this long, a page has likely missed events or lost its stream (Android freezes it). */
export const STALE_MS = 30_000;

/** Called once from main.tsx: refresh silently on coming back to the app after 30 s, on the
 * network returning, and on a page restored from the back-forward cache. */
export function startResync(now = () => Date.now()) {
  let hiddenAt: number | null = null;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") hiddenAt = now();
    else if (hiddenAt !== null && now() - hiddenAt > STALE_MS) void refresh();
    if (document.visibilityState === "visible") hiddenAt = null;
  });
  addEventListener("online", () => void refresh());
  addEventListener("pageshow", (e) => (e as PageTransitionEvent).persisted && void refresh());
}
