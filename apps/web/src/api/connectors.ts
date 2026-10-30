import { useEffect, useState } from "react";

import { api } from "./client";
import type { components } from "./schema";

export type Connector = components["schemas"]["ConnectorView"];
export type Connectors = Record<string, Connector>;

/** A world's connectors by name, as the API describes them: label, kind, Capabilities and what
 * each makes, from its Manifest (ADR 0025). Empty until they load, or when they cannot. */
export function useConnectors(worldId: string): Connectors {
  const [byName, setByName] = useState<Connectors>({});
  useEffect(() => {
    let stale = false;
    api
      .GET("/api/worlds/{world_id}/connectors", { params: { path: { world_id: worldId } } })
      .then(({ data }) => !stale && data && setByName(Object.fromEntries(data.map((c) => [c.name, c]))))
      .catch(() => undefined);
    return () => {
      stale = true;
    };
  }, [worldId]);
  return byName;
}

/** What a person reads for a connector: its label, else its name. */
export const labelOf = (connectors: Connectors, name: string) => connectors[name]?.label ?? name;
