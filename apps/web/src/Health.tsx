import { useEffect, useState } from "react";

import { api } from "./api/client";

type State = { kind: "loading" } | { kind: "ok"; version: string } | { kind: "down" };

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

  if (state.kind === "loading") return <p role="status">Checking the API...</p>;
  if (state.kind === "down") return <p role="alert">API is not reachable.</p>;
  return (
    <p role="status">
      API is up, version <span data-testid="version">{state.version}</span>
    </p>
  );
}
