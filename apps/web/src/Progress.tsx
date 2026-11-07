import { useEffect, useState, useSyncExternalStore } from "react";

import { requestsInFlight, watchRequests } from "./api/client";

/** Fast calls never flash the line. */
const DELAY = 150;
/** How long the finishing fill and fade run, the same as `--dur-base`. */
const FINISH = 200;

/** The 2px accent line at the top edge: it shows once API calls have been in flight for 150 ms
 * and finishes with a quick fill and fade when the last one returns. */
export function Progress() {
  const busy = useSyncExternalStore(watchRequests, () => requestsInFlight() > 0);
  const [state, setState] = useState<"off" | "on" | "done">("off");
  useEffect(() => {
    if (busy) {
      const t = setTimeout(() => setState("on"), DELAY);
      return () => clearTimeout(t);
    }
    setState((s) => (s === "on" ? "done" : "off"));
    const t = setTimeout(() => setState("off"), FINISH);
    return () => clearTimeout(t);
  }, [busy]);
  return state === "off" ? null : <ProgressLine done={state === "done"} />;
}

/** The line itself; also the router's pending screen while a lazy route loads. */
export const ProgressLine = ({ done = false }: { done?: boolean }) => (
  <div className={done ? "route-progress done" : "route-progress"} data-testid="route-progress" aria-hidden="true" />
);
