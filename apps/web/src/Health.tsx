import { useEffect, useState } from "react";

import { api } from "./api/client";

type State = { kind: "loading" } | { kind: "ok"; version: string } | { kind: "down" };

/** The sidebar's health line: whether the API answers, and its version. */
export function Health() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;
    api
      .GET("/api/health")
      .then(({ data }) => {
        if (!cancelled) setState(data ? { kind: "ok", version: data.version } : { kind: "down" });
      })
      .catch(() => {
        if (!cancelled) setState({ kind: "down" });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Plain text, not a live region: it sits on every screen beside the page's own status.
  return (
    <div className="nav-health" data-testid="health">
      <span className={`dot ${state.kind === "ok" ? "live" : state.kind === "down" ? "bad" : ""}`} />
      {state.kind === "loading" && "Checking the API..."}
      {state.kind === "down" && "API is not reachable"}
      {state.kind === "ok" && (
        <span>
          API up, version <span data-testid="version">{state.version}</span>
        </span>
      )}
    </div>
  );
}
