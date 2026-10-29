import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";

type Models = components["schemas"]["Models"];

/** The models the API prices and the global default; null until loaded. */
export function useModels(): Models | null {
  const [models, setModels] = useState<Models | null>(null);
  useEffect(() => {
    api
      .GET("/api/models")
      .then(({ data }) => setModels(data ?? null))
      .catch(() => undefined);
  }, []);
  return models;
}

/** A model picker whose empty value falls back to `fallback`; "" means no choice here. A hidden
 * label still names it, for a table cell whose column says what it is. */
export function ModelSelect(props: {
  label: string;
  hideLabel?: boolean;
  value: string;
  fallback: string;
  models: Models | null;
  onChange: (value: string) => void;
}) {
  const known = props.models?.models ?? [];
  const extra = props.value && !known.includes(props.value) ? [props.value] : [];
  return (
    <label>
      <span className={props.hideLabel ? "visually-hidden" : undefined}>{props.label}</span>
      <select value={props.value} onChange={(e) => props.onChange(e.target.value)}>
        <option value="">{props.fallback}</option>
        {[...known, ...extra].map((m) => (
          <option key={m} value={m}>
            {m}
          </option>
        ))}
      </select>
    </label>
  );
}
