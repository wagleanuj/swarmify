import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { connectorsRoute } from "../router";

type Connector = components["schemas"]["ConnectorView"];
type HealthCheck = components["schemas"]["HealthCheck"];
type KeyCheck = components["schemas"]["KeyCheck"];
type NewConnector = components["schemas"]["NewConnector"];
type Tool = components["schemas"]["Tool"];
type World = components["schemas"]["World"];
type Kind = NewConnector["kind"];
type StageType = NewConnector["stage_types"][number];

/** Skin labels for the built-ins (ADR 0004); code keeps the plain names. */
export const LABEL: Record<string, string> = {
  web_search: "Web search",
  graph: "Middle-out",
  remotion_render: "Remotion render",
  remotion_build: "Remotion build",
  youtube: "YouTube",
  upload_post: "Upload-Post",
  speech: "Speech",
};

const KINDS: { kind: Kind; label: string; hint: string }[] = [
  { kind: "stage", label: "Stage", hint: "A connector stage calls it once per run." },
  { kind: "tool", label: "Tool", hint: "Agents may use its tools during swarm stages." },
  { kind: "publish", label: "Publish", hint: "Posts to the outside world. Always after a gate." },
];

/** `NAME=value` per line; a bare `NAME` means the keys file already holds it. */
export function parseKeys(text: string): Record<string, string> {
  const keys: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const at = trimmed.indexOf("=");
    if (at === -1) keys[trimmed] = "";
    else keys[trimmed.slice(0, at).trim()] = trimmed.slice(at + 1).trim();
  }
  return keys;
}

function status(c: Connector): { text: string; tone: "ok" | "warn" | "off" } {
  if (c.missing.length > 0) return { text: `${c.missing.join(", ")} missing`, tone: "warn" };
  // remotion_build runs inside swarmify (a session, then the render), so it has no server
  if (c.builtin && c.kind !== "tool" && c.command.length === 0 && !c.url && c.name !== "remotion_build") {
    return { text: "not installed", tone: "off" };
  }
  return { text: "ready", tone: "ok" };
}

function where(c: Connector): string {
  if (c.builtin) return c.about;
  const server = c.url ?? c.command.join(" ");
  return c.credentials.length ? `${server} · ${c.credentials.join(", ")}` : `${server} · no key`;
}

export function Connectors() {
  const { worldId } = connectorsRoute.useParams();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("stage");
  const [server, setServer] = useState("");
  const [keys, setKeys] = useState("");
  const [allowed, setAllowed] = useState<StageType[]>(["connector"]);
  const [tested, setTested] = useState<{ body: string; tools: Tool[] } | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const stageTypes: StageType[] = kind === "publish" ? ["connector"] : allowed;
  const body: NewConnector = { name, kind, server, credentials: parseKeys(keys), stage_types: stageTypes };
  const current = JSON.stringify(body);
  const tools = tested?.body === current ? tested.tools : null;

  function load() {
    return api
      .GET("/api/worlds/{world_id}/connectors", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setConnectors(data));
  }

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data, response }) => setWorld(response.status === 404 ? null : (data ?? null)))
      .catch(() => setError("Could not load this world. Is the API running?"));
    void load().catch(() => undefined);
  }, [worldId]);

  async function test(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy("test");
    setError(null);
    const { data, error } = await api.POST("/api/worlds/{world_id}/connectors/test", {
      params: { path: { world_id: worldId } },
      body,
    });
    setBusy(null);
    if (error || !data) {
      setTested(null);
      setError(detail(error) ?? "Could not start it. Check the command and try again.");
      return;
    }
    setTested({ body: current, tools: data.tools });
  }

  async function save() {
    setBusy("save");
    setError(null);
    const { error } = await api.POST("/api/worlds/{world_id}/connectors", {
      params: { path: { world_id: worldId } },
      body,
    });
    setBusy(null);
    if (error) {
      setError(detail(error) ?? "Could not save the connector. Try again.");
      return;
    }
    setName("");
    setServer("");
    setKeys("");
    setTested(null);
    await load();
  }

  async function remove(c: Connector) {
    if (!window.confirm(`Remove ${c.name}? Workflows that call it stop passing their checks.`)) return;
    const { error } = await api.DELETE("/api/connectors/{connector_id}", {
      params: { path: { connector_id: c.id } },
    });
    if (error) {
      setError("Could not remove it. Try again.");
      return;
    }
    await load();
  }

  if (world === null) {
    return (
      <main>
        <h1>No such world</h1>
        <p className="sub">
          It may have been deleted. <Link to="/">Back to your worlds.</Link>
        </p>
      </main>
    );
  }

  return (
    <main>
      <header className="page">
        <div>
          <h1>Connectors</h1>
          <p className="sub">
            Each connector is an MCP server, its keys, and which stage types may use it. Keys live
            in ~/.swarmify/.env, never inside a run or a shared workflow, and are shared by every
            world: changing a key here changes it for all your worlds.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <ul className="connector-list" aria-label="Connectors" data-testid="connector-list">
        {connectors.map((c) => {
          const s = status(c);
          return (
            <li key={c.id} data-name={c.name}>
              <span className="connector-main">
                <b>{LABEL[c.name] ?? c.name}</b>
                <span className="faint mono">{where(c)}</span>
              </span>
              <span className="chips">
                {c.stage_types.map((t) => (
                  <span key={t} className="chip">
                    {t}
                  </span>
                ))}
                {c.kind === "publish" && <span className="chip warn">gate first, always</span>}
                <span className={`chip ${s.tone}`}>{s.text}</span>
                {c.credentials.length > 0 && <SetKeys connector={c} onSaved={load} onError={setError} />}
                {!c.builtin && (
                  <button type="button" onClick={() => remove(c)} aria-label={`Remove ${c.name}`}>
                    Remove
                  </button>
                )}
              </span>
              {c.health && <Health connector={c} />}
            </li>
          );
        })}
      </ul>

      <form className="card add-connector" onSubmit={test} aria-label="Add a connector">
        <h2>Add a connector</h2>
        <p className="faint">
          Paste an MCP command or URL. Swarmify starts it once, lists its tools, and shows you what
          it will be allowed to do before saving.
        </p>
        <label>
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            maxLength={60}
            placeholder="fal_video"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label>
          MCP server
          <input
            value={server}
            onChange={(e) => setServer(e.target.value)}
            required
            placeholder="uvx fal-mcp   or   https://host/mcp"
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        <label>
          Keys, one NAME=value per line (stored in ~/.swarmify/.env, mode 600)
          <textarea
            value={keys}
            onChange={(e) => setKeys(e.target.value)}
            rows={2}
            placeholder="FAL_KEY=..."
            spellCheck={false}
          />
        </label>
        <fieldset className="kinds">
          <legend>What it does</legend>
          {KINDS.map((k) => (
            <label key={k.kind} className={kind === k.kind ? "kind picked" : "kind"}>
              <input
                type="radio"
                name="kind"
                value={k.kind}
                checked={kind === k.kind}
                onChange={() => setKind(k.kind)}
              />
              <b>{k.label}</b>
              <span className="faint">{k.hint}</span>
            </label>
          ))}
        </fieldset>
        <fieldset className="allowed">
          <legend>Allowed in</legend>
          {(["swarm", "connector"] as const).map((t) => (
            <label key={t} className="check">
              <input
                type="checkbox"
                checked={stageTypes.includes(t)}
                disabled={kind === "publish"}
                onChange={(e) =>
                  setAllowed((prev) =>
                    e.target.checked ? [...prev, t] : prev.filter((p) => p !== t),
                  )
                }
              />
              {t} stages
            </label>
          ))}
        </fieldset>
        <div className="editor-actions">
          <button type="submit" disabled={busy !== null || stageTypes.length === 0}>
            {busy === "test" ? "Starting it..." : "Test and list tools"}
          </button>
          {tools && (
            <button type="button" className="primary" onClick={save} disabled={busy !== null}>
              {busy === "save" ? "Saving..." : "Save connector"}
            </button>
          )}
        </div>
        {tools && (
          <section aria-label="Tools" data-testid="tools">
            <p className="ok">
              It started and offers {tools.length} {tools.length === 1 ? "tool" : "tools"}. Once
              saved, {stageTypes.map((t) => `${t} stages`).join(" and ")} may call{" "}
              {tools.length === 1 ? "it" : "them"}.
            </p>
            <ul className="tool-list">
              {tools.map((t) => (
                <li key={t.name}>
                  <code>{t.name}</code> <span className="faint">{t.description}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </form>
    </main>
  );
}

function detail(error: unknown): string | null {
  const d = (error as { detail?: unknown } | undefined)?.detail;
  return typeof d === "string" ? d : null;
}

/** What a connector depends on, such as each speech engine, checked once when the page opens. */
function Health({ connector }: { connector: Connector }) {
  const [checks, setChecks] = useState<HealthCheck[] | string | null>(null);

  useEffect(() => {
    api
      .GET("/api/connectors/{connector_id}/health", { params: { path: { connector_id: connector.id } } })
      .then(({ data, error }) => setChecks(data ?? detail(error) ?? "Could not check them."))
      .catch(() => setChecks("Could not check them."));
  }, [connector.id]);

  if (checks === null) return <p className="faint health">Checking engines...</p>;
  if (typeof checks === "string") return <p className="faint health">Engines not checked: {checks}</p>;
  return (
    <ul className="health" aria-label={`${LABEL[connector.name] ?? connector.name} engines`}>
      {checks.map((h) => (
        <li key={h.name}>
          <span className="mono">{h.name}</span>{" "}
          <span className={`chip ${h.ok ? "ok" : "warn"}`}>{h.ok ? "up" : "down"}</span>
          {!h.ok && <span className="faint"> {h.message}</span>}
        </li>
      ))}
    </ul>
  );
}

/** One password field per credential the connector names; empty fields leave the file alone. */
function SetKeys({
  connector,
  onSaved,
  onError,
}: {
  connector: Connector;
  onSaved: () => Promise<unknown>;
  onError: (message: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const [check, setCheck] = useState<KeyCheck | null>(null);
  const label = LABEL[connector.name] ?? connector.name;

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fields = new FormData(e.currentTarget);
    const credentials = Object.fromEntries(connector.credentials.map((n) => [n, String(fields.get(n) ?? "").trim()]));
    onError(null);
    setCheck(null);
    const { data, error } = await api.PUT("/api/connectors/{connector_id}/credentials", {
      params: { path: { connector_id: connector.id } },
      body: { credentials },
    });
    if (error) {
      onError(detail(error) ?? `Could not save the keys for ${label}. Try again.`);
      return;
    }
    setOpen(false);
    setCheck(data?.check ?? null);
    await onSaved();
  }

  if (!open) {
    return (
      <>
        <button type="button" onClick={() => setOpen(true)} aria-label={`Set keys for ${connector.name}`}>
          {connector.missing.length > 0 ? "Add keys" : "Change keys"}
        </button>
        {check?.ok && (
          <span role="status" className="key-check">
            Keys saved. {check.message}
          </span>
        )}
        {check && !check.ok && (
          <span role="alert" className="key-check">
            Keys saved, but the check failed: {check.message.replace(/\.$/, "")}. Publishing fails until they work.
          </span>
        )}
      </>
    );
  }
  return (
    <form className="set-keys" onSubmit={save} aria-label={`Keys for ${connector.name}`}>
      {connector.credentials.map((n) => (
        <label key={n}>
          {n}
          {connector.missing.includes(n) ? "" : " (saved; leave empty to keep it)"}
          <input name={n} type="password" autoComplete="off" spellCheck={false} />
        </label>
      ))}
      <div className="row">
        <button type="submit" className="primary">
          Save keys
        </button>
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <span className="faint">
          Stored in ~/.swarmify/.env, mode 600, shared by every world. Never shown again.
        </span>
      </div>
    </form>
  );
}
