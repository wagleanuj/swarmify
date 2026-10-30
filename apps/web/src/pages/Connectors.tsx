import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { connectorsRoute } from "../router";

type Connector = components["schemas"]["ConnectorView"];
type HealthCheck = components["schemas"]["HealthCheck"];
type KeyCheck = components["schemas"]["KeyCheck"];
type SetupField = components["schemas"]["SetupField"];
type SetupView = components["schemas"]["SetupView"];
type NewConnector = components["schemas"]["NewConnector"];
type Package = components["schemas"]["Package"];
type PackageSwitch = components["schemas"]["PackageSwitch"];
type PackageReview = components["schemas"]["PackageReview"];
type Tool = components["schemas"]["Tool"];
type World = components["schemas"]["World"];
type Kind = NewConnector["kind"];
type StageType = NewConnector["stage_types"][number];

const UTC_TIME = /^\d{4}-\d\d-\d\dT\d\d:\d\d(:\d\d)?Z$/;

/** A value a publish connector sends, as plain copy: a UTC time in local time with the UTC
 * beside it, yes or no, a list joined, and the connector's hint after it (or instead, when
 * there is no value). */
export function PublishValue({ value, hint }: { value?: unknown; hint?: string }) {
  const note = hint ? <span className="faint"> {hint}</span> : null;
  if (value === null || value === undefined || value === "") return <>{hint || "None"}</>;
  if (typeof value === "string" && UTC_TIME.test(value)) {
    return (
      <>
        <time dateTime={value}>{new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</time>{" "}
        <span className="faint">({value.slice(0, 16).replace("T", " ")} UTC).</span>
        {note}
      </>
    );
  }
  const text = typeof value === "boolean" ? (value ? "Yes" : "No") : Array.isArray(value) ? value.join(", ") : typeof value === "object" ? JSON.stringify(value) : String(value);
  return (
    <>
      {text}
      {note}
    </>
  );
}

const KINDS: { kind: Kind; label: string; hint: string }[] = [
  { kind: "stage", label: "Stage", hint: "A connector stage calls it once per run." },
  { kind: "tool", label: "Tool", hint: "Agents may use its tools during swarm stages." },
  { kind: "publish", label: "Publish", hint: "Posts to the outside world. Always after a gate." },
];

const GROUPS: [Connector["kind"], string, string][] = [
  ["tool", "Tools agents use inside a swarm step", ""],
  ["stage", "Steps of their own", ""],
  ["publish", "Publishing", "Always after a gate."],
  ["notify", "Notifications", "Answer gates from outside Swarmify."],
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

export function status(c: Connector): { text: string; tone: "ok" | "warn" } {
  if (c.sign_in.length > 0) return { text: "Needs sign-in", tone: "warn" };
  if (c.missing.length > 0) return { text: "Needs setup", tone: "warn" };
  return { text: "Ready", tone: "ok" };
}

const where = (c: Connector) => `${c.about} · package ${c.name} ${c.version}`;

export function Connectors() {
  const { worldId } = connectorsRoute.useParams();
  const [world, setWorld] = useState<World | null | undefined>(undefined);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [packages, setPackages] = useState<Package[]>([]);
  const [enabled, setEnabled] = useState<Record<string, string>>({});
  const [switching, setSwitching] = useState<{ p: Package; diff: PackageSwitch } | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("stage");
  const [server, setServer] = useState("");
  const [keys, setKeys] = useState("");
  const [network, setNetwork] = useState("");
  const [allowed, setAllowed] = useState<StageType[]>(["connector"]);
  const [tested, setTested] = useState<{ body: string; tools: Tool[] } | null>(null);
  const [busy, setBusy] = useState<"test" | "save" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installed, setInstalled] = useState<string | null>(null);
  // Where an OAuth sign-in lands: the callback sends the browser back here saying how it went.
  const search = connectorsRoute.useSearch();
  const navigate = useNavigate();
  const [signIn] = useState(() =>
    search.sign_in
      ? { ok: true, message: `Signed in. ${search.sign_in} gets a fresh access token each time it starts.` }
      : search.sign_in_error
        ? { ok: false, message: search.sign_in_error }
        : null,
  );
  useEffect(() => {
    if (signIn) void navigate({ to: ".", search: {}, replace: true });
  }, [signIn, navigate]);

  const stageTypes: StageType[] = kind === "publish" ? ["connector"] : allowed;
  const hosts = network.split(/[\s,]+/).filter(Boolean);
  const body: NewConnector = { name, kind, server, credentials: parseKeys(keys), stage_types: stageTypes, network: hosts };
  const current = JSON.stringify(body);
  const tools = tested?.body === current ? tested.tools : null;

  function load() {
    return api
      .GET("/api/worlds/{world_id}/connectors", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setConnectors(data));
  }

  function loadPackages() {
    return api.GET("/api/packages").then(({ data }) => data && setPackages(data));
  }

  function loadEnabled() {
    return api
      .GET("/api/worlds/{world_id}/packages", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setEnabled(Object.fromEntries(data.map((e) => [e.name, e.version]))));
  }

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data, response }) => setWorld(response.status === 404 ? null : (data ?? null)))
      .catch(() => setError("Could not load this world. Is the API running?"));
    void load().catch(() => undefined);
    void loadEnabled().catch(() => undefined);
    void loadPackages().catch(() => undefined);
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
    setNetwork("");
    setTested(null);
    await Promise.all([load(), loadEnabled(), loadPackages()]);
  }

  /** A switch of the pinned version first shows what changes in the Manifest. */
  async function askSwitch(p: Package) {
    setError(null);
    const { data, error } = await api.GET("/api/worlds/{world_id}/packages/{name}/switch", {
      params: { path: { world_id: worldId, name: p.name }, query: { version: p.version } },
    });
    if (error || !data) {
      setError(detail(error) ?? `Could not read what ${p.label} ${p.version} changes. Try again.`);
      return;
    }
    setSwitching({ p, diff: data });
  }

  async function uninstall(p: Package) {
    if (!window.confirm(`Uninstall ${p.name} ${p.version}? Its folder is removed for every world.`)) return;
    setError(null);
    const { error } = await api.DELETE("/api/packages/{name}/{version}", {
      params: { path: { name: p.name, version: p.version } },
    });
    if (error) {
      setError(detail(error) ?? `Could not uninstall ${p.name} ${p.version}. Try again.`);
      return;
    }
    await loadPackages();
  }

  async function enable(p: Package) {
    setError(null);
    setSwitching(null);
    const { error } = await api.PUT("/api/worlds/{world_id}/packages/{name}", {
      params: { path: { world_id: worldId, name: p.name } },
      body: { version: p.version },
    });
    if (error) {
      setError(detail(error) ?? `Could not enable ${p.label}. Try again.`);
      return;
    }
    await Promise.all([loadEnabled(), load()]);
  }

  async function disable(p: Package) {
    if (!window.confirm(`Disable ${p.label} in this world? Its workflows that call ${p.name} stop passing their checks. Other worlds keep it.`)) return;
    const { error } = await api.DELETE("/api/worlds/{world_id}/packages/{name}", {
      params: { path: { world_id: worldId, name: p.name } },
    });
    if (error) {
      setError(`Could not disable ${p.label}. Try again.`);
      return;
    }
    await Promise.all([loadEnabled(), load()]);
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

  const waiting = connectors.filter((c) => c.missing.length > 0).length;
  return (
    <main>
      <header className="page">
        <div>
          <h1>Connectors</h1>
          <p className="sub">
            {connectors.length} connectors, {waiting} waiting on a key. Any MCP server can be one: Swarmify
            installs it as a local package with its keys in its Setup. No key ever enters a run or a shared workflow.
            Web search and the world's graph are not connectors: they are Agent Tools Swarmify gives swarm steps,
            with nothing to set up.
          </p>
        </div>
        <div className="actions">
          <button type="button" aria-expanded={installing} onClick={() => setInstalling((i) => !i)}>
            Install a package
          </button>
          <button type="button" aria-expanded={adding} onClick={() => setAdding((a) => !a)}>
            Add a connector
          </button>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {installed && <p role="status">{installed}</p>}
      {installing && (
        <InstallPackage
          onInstalled={(p) => {
            setInstalling(false);
            setInstalled(`Installed ${p.label} ${p.version}. Enable it below in each world that uses it.`);
            void loadPackages();
          }}
        />
      )}
      {signIn && <p role={signIn.ok ? "status" : "alert"}>{signIn.message}</p>}
      {adding && (
      <form className="panel add-connector" onSubmit={test} aria-label="Add a connector">
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
          Keys, one NAME=value per line (saved as its Setup's Credentials, mode 600)
          <textarea
            value={keys}
            onChange={(e) => setKeys(e.target.value)}
            rows={2}
            placeholder="FAL_KEY=..."
            spellCheck={false}
          />
        </label>
        <label>
          Network domains it reaches, comma separated (its sandbox blocks every other host; npx and uvx also reach their registry)
          <input
            value={network}
            onChange={(e) => setNetwork(e.target.value)}
            placeholder="api.fal.ai, *.fal.media"
            autoComplete="off"
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
      )}
      <div className="con-groups" data-testid="connector-list">
        {GROUPS.map(([k, title, note]) => {
          const group = connectors.filter((c) => c.kind === k);
          if (group.length === 0) return null;
          return (
            <section key={k} className="panel" aria-labelledby={`group-${k}`}>
              <div className="panel-head">
                <h2 id={`group-${k}`}>{title}</h2>
                <span className="faint">
                  {note} <span className="num">{group.length}</span>
                </span>
              </div>
              <ul className="con-list" aria-label={title}>
                {group.map((c) => {
                  const s = status(c);
                  return (
                    <li key={c.id} data-name={c.name} className="con-row">
                      <span className={s.tone === "ok" ? "dot live" : s.tone === "warn" ? "dot warn" : "dot"} />
                      <span className="con-main">
                        <b>{c.label}</b>
                        <span className="faint">{where(c)}</span>
                      </span>
                      <span className="faint">{c.stage_types.length > 0 ? `${c.stage_types.join(", ")} steps` : "gates"}</span>
                      <span className={s.tone === "warn" ? "pill warning" : "pill"}>{s.text}</span>
                      <Setup connector={c} worldId={worldId} onSaved={load} />
                      {(c.health || c.services.length > 0) && <Health connector={c} worldId={worldId} />}
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
        {packages.length > 0 && (
          <section className="panel" aria-labelledby="group-packages">
            <div className="panel-head">
              <h2 id="group-packages">Installed packages</h2>
              <span className="faint">
                Installed once for every world; this world uses the ones it enables, at the version it pins.{" "}
                <span className="num">{packages.length}</span>
              </span>
            </div>
            <ul className="con-list" aria-label="Installed packages">
              {packages.map((p) => (
                <li key={`${p.name}@${p.version}`} data-package={p.name} className="con-row">
                  <span className={p.state === "installed" ? "dot live" : "dot warn"} />
                  <span className="con-main">
                    <b>{p.label}</b>
                    <span className="faint">{p.problem ?? p.about}</span>
                  </span>
                  <span className="faint mono">
                    {p.name} {p.version}
                  </span>
                  {p.kind && <span className="faint">{p.kind}</span>}
                  <span className={p.state === "installed" ? "pill" : "pill warning"}>
                    {p.state === "installed" ? "Installed" : "Invalid Manifest"}
                  </span>
                  {enabled[p.name] === p.version ? (
                    <>
                      <span className="pill">Enabled here</span>
                      <button type="button" onClick={() => disable(p)} aria-label={`Disable ${p.name} ${p.version}`}>
                        Disable
                      </button>
                    </>
                  ) : (
                    p.state === "installed" && (
                      <button
                        type="button"
                        onClick={() => (enabled[p.name] ? askSwitch(p) : enable(p))}
                        aria-label={`Enable ${p.name} ${p.version}`}
                      >
                        {enabled[p.name] ? `Switch from ${enabled[p.name]}` : "Enable"}
                      </button>
                    )
                  )}
                  <button type="button" onClick={() => uninstall(p)} aria-label={`Uninstall ${p.name} ${p.version}`}>
                    Uninstall
                  </button>
                  {switching?.p === p && (
                    <SwitchDiff
                      p={p}
                      diff={switching.diff}
                      worldId={worldId}
                      onSwitch={() => enable(p)}
                      onCancel={() => setSwitching(null)}
                      onSetUp={() => askSwitch(p)}
                    />
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </main>
  );
}

const PERMISSION_KINDS: [string, string][] = [
  ["network", "Hosts it reaches"],
  ["install_network", "Hosts its install step reaches"],
  ["folders", "Data folders it reads"],
  ["services", "Services it calls"],
  ["owned_folders", "World folders it owns"],
  ["credentials", "Credentials it is given"],
  ["sign_in", "Sign-in hosts Swarmify calls for it"],
  ["capabilities", "Capabilities it uses"],
];

/** Install a Connector Package from a git URL at a ref, or a local folder: Swarmify fetches it
 * running nothing, shows every permission it declares (what an upgrade adds marked new), and
 * installs only once the operator approves them (ADR 0021). */
function InstallPackage({ onInstalled }: { onInstalled: (p: Package) => void }) {
  const [source, setSource] = useState("");
  const [ref, setRef] = useState("");
  const [review, setReview] = useState<{ asked: string; r: PackageReview } | null>(null);
  const [busy, setBusy] = useState<"review" | "install" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const body = { source: source.trim(), ref: ref.trim() || null };
  const shown = review?.asked === JSON.stringify(body) ? review.r : null;

  async function ask(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    setBusy("review");
    setError(null);
    const { data, error } = await api.POST("/api/packages/review", { body });
    setBusy(null);
    if (error || !data) {
      setReview(null);
      setError(detail(error) ?? "Could not fetch it. Check the URL and ref and try again.");
      return;
    }
    setReview({ asked: JSON.stringify(body), r: data });
  }

  async function approve(r: PackageReview) {
    setBusy("install");
    setError(null);
    const { data, error } = await api.POST("/api/packages", {
      body: { ...body, commit: r.commit, approved: r.permissions },
    });
    setBusy(null);
    if (error || !data) {
      setError(detail(error) ?? `Could not install ${r.label} ${r.version}. Try again.`);
      return;
    }
    onInstalled(data);
  }

  const added = new Set(shown?.upgrade?.added ?? []);
  const groups = PERMISSION_KINDS.map(([kind, label]) => ({
    label,
    values: (shown?.permissions ?? []).filter((p) => p.startsWith(`${kind}: `)),
  })).filter((g) => g.values.length > 0);
  return (
    <form className="panel add-connector" onSubmit={ask} aria-label="Install a package">
      <h2>Install a package</h2>
      <p className="faint">
        From a git URL at a tag, branch or commit, or a folder on this machine. Swarmify fetches it without running
        any of it and shows everything it asks for; nothing of it runs, its install step included, until you approve.
      </p>
      <label>
        Git URL or folder
        <input
          value={source}
          onChange={(e) => setSource(e.target.value)}
          required
          maxLength={1000}
          placeholder="https://github.com/you/your-connector.git"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <label>
        Tag, branch or commit (empty for the default branch)
        <input
          value={ref}
          onChange={(e) => setRef(e.target.value)}
          maxLength={200}
          placeholder="v1.0.0"
          autoComplete="off"
          spellCheck={false}
        />
      </label>
      <div className="editor-actions">
        <button type="submit" disabled={busy !== null}>
          {busy === "review" ? "Fetching it..." : "Review permissions"}
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
      {shown && (
        <section className="con-switch" aria-label={`Permissions of ${shown.name} ${shown.version}`}>
          <h3>
            {shown.label} {shown.version} asks for
          </h3>
          <p className="faint">
            {shown.about}
            {shown.commit && (
              <>
                {" "}
                At commit <span className="mono">{shown.commit.slice(0, 12)}</span>.
              </>
            )}
          </p>
          {shown.upgrade &&
            (added.size > 0 ? (
              <p role="status" className="warning">
                New since {shown.upgrade.from}: {[...added].join(", ")}. Installing {shown.version} grants{" "}
                {added.size === 1 ? "it" : "them"} too.
              </p>
            ) : (
              <p role="status">Nothing new since {shown.upgrade.from}: the permissions you approved for it.</p>
            ))}
          {shown.upgrade && shown.upgrade.removed.length > 0 && (
            <p className="faint">No longer asks for: {shown.upgrade.removed.join(", ")}.</p>
          )}
          {groups.length === 0 ? (
            <p>It asks for no permissions: no network, folders, Services or Credentials.</p>
          ) : (
            <dl>
              {groups.map((g) => (
                <div key={g.label}>
                  <dt>{g.label}</dt>
                  <dd className="mono">
                    {g.values.map((v, i) => (
                      <span key={v}>
                        {i > 0 && ", "}
                        {v.startsWith("capabilities: ") ? v.slice(14).replaceAll("_", " ") : v.slice(v.indexOf(": ") + 2)}
                        {added.has(v) && <b className="warning"> (new)</b>}
                      </span>
                    ))}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <div className="actions">
            <button type="button" className="primary" disabled={busy !== null} onClick={() => approve(shown)}>
              {busy === "install"
                ? "Installing..."
                : shown.upgrade && added.size === 0
                  ? `Install ${shown.version}`
                  : `Approve and install ${shown.version}`}
            </button>
            <button type="button" onClick={() => setReview(null)}>
              Cancel
            </button>
          </div>
        </section>
      )}
    </form>
  );
}

/** What moving the world's pin changes in the package's Manifest, held back while a newly
 * required Setup field has no value; the new version's Setup form fills it in. */
function SwitchDiff({
  p,
  diff,
  worldId,
  onSwitch,
  onCancel,
  onSetUp,
}: {
  p: Package;
  diff: PackageSwitch;
  worldId: string;
  onSwitch: () => void;
  onCancel: () => void;
  onSetUp: () => Promise<unknown>;
}) {
  const groups = [
    { label: "Params", c: diff.params },
    { label: "Setup fields", c: diff.setup },
    { label: "Permissions", c: diff.permissions },
  ].filter(({ c }) => c.added.length + c.removed.length + (c.changed?.length ?? 0) > 0);
  return (
    <section className="con-switch" aria-label={`Switch ${p.name} to ${p.version}`}>
      <h3>
        Switch {p.label} from {diff.from} to {diff.to}
      </h3>
      <p className="faint">
        Workflow versions saved on {diff.from} keep running it; versions you save after the switch use {diff.to}.
      </p>
      {groups.length === 0 ? (
        <p>Its params, Setup fields and permissions are unchanged.</p>
      ) : (
        <dl>
          {groups.map(({ label, c }) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd className="mono">
                {(
                  [
                    ["Added", c.added],
                    ["Removed", c.removed],
                    ["Changed", c.changed ?? []],
                  ] as const
                )
                  .filter(([, names]) => names.length > 0)
                  .map(([what, names]) => `${what} ${names.join(", ")}`)
                  .join(". ")}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {diff.needs.length > 0 && (
        <>
          <p role="status" className="warning">
            Fill in {diff.needs.join(", ")} in {p.label} {diff.to}'s Setup first; the switch waits for{" "}
            {diff.needs.length === 1 ? "it" : "them"}.
          </p>
          <div className="row">
            <Setup
              connector={{ name: p.name, label: `${p.label} ${diff.to}`, missing: diff.needs }}
              worldId={worldId}
              version={diff.to}
              onSaved={onSetUp}
            />
          </div>
        </>
      )}
      <div className="actions">
        <button type="button" className="primary" disabled={diff.needs.length > 0} onClick={onSwitch}>
          Switch to {diff.to}
        </button>
        <button type="button" onClick={onCancel}>
          Keep {diff.from}
        </button>
      </div>
    </section>
  );
}

function detail(error: unknown): string | null {
  const d = (error as { detail?: unknown } | undefined)?.detail;
  return typeof d === "string" ? d : null;
}

/** What a connector depends on, such as each of a package's Services (an engine it calls),
 * checked once when the page opens. Swarmify never starts them; the operator does. */
function Health({ connector, worldId }: { connector: Connector; worldId: string }) {
  const [checks, setChecks] = useState<HealthCheck[] | string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/packages/{name}/health", { params: { path: { world_id: worldId, name: connector.name } } })
      .then(({ data, error }) => setChecks(data ?? detail(error) ?? "Could not check them."))
      .catch(() => setChecks("Could not check them."));
  }, [connector.name, worldId]);

  if (checks === null) return <p className="faint health">Checking services...</p>;
  if (typeof checks === "string") return <p className="faint health">Services not checked: {checks}</p>;
  return (
    <ul className="health" aria-label={`${connector.label} services`}>
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

/** The control for one Setup field's value in one scope, named `<scope>:<key>`. */
function FieldInput({ f, scope, required }: { f: SetupField; scope: "shared" | "world"; required: boolean }) {
  const saved = scope === "shared" ? f.shared : f.world;
  const set = scope === "shared" ? f.shared_set : f.world_set;
  const name = `${scope}:${f.key}`;
  const value = saved === null || saved === undefined ? "" : String(saved);
  const fallback = scope === "world" && f.scope === "shared" ? "Use the shared value" : f.default !== null ? `Default: ${f.default === true ? "Yes" : f.default === false ? "No" : f.default}` : "Not set";
  if (f.type === "boolean" || f.type === "choice") {
    const options: [string, string][] = f.type === "boolean" ? [["true", "Yes"], ["false", "No"]] : (f.options ?? []).map((o) => [o, o]);
    return (
      <select name={name} defaultValue={value} required={required}>
        <option value="">{fallback}</option>
        {options.map(([v, label]) => (
          <option key={v} value={v}>
            {label}
          </option>
        ))}
      </select>
    );
  }
  const placeholder = f.secret && set ? "Saved; leave empty to keep it" : fallback === "Not set" || fallback.startsWith("Use") ? "" : fallback;
  if (f.type === "text") return <textarea name={name} defaultValue={value} rows={3} required={required} placeholder={placeholder} />;
  const type = f.secret ? "password" : f.type === "url" ? "url" : f.type === "number" || f.type === "integer" ? "number" : "text";
  return (
    <input
      name={name}
      type={type}
      defaultValue={value}
      required={required}
      placeholder={placeholder}
      step={f.type === "integer" ? 1 : f.type === "number" ? "any" : undefined}
      min={f.min ?? undefined}
      max={f.max ?? undefined}
      pattern={f.pattern ?? undefined}
      autoComplete="off"
      spellCheck={false}
    />
  );
}

/** A package's Setup form, drawn from its Manifest's fields (ADR 0024). Its test runs with the
 * candidate values before anything is saved, and what it said is shown either way. */
function Setup({
  connector,
  worldId,
  onSaved,
  version,
}: {
  connector: Pick<Connector, "name" | "label" | "missing">;
  worldId: string;
  onSaved: () => Promise<unknown>;
  /** An installed version other than the pinned one: its form, filled in before a switch. */
  version?: string;
}) {
  const [form, setForm] = useState<SetupView | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<KeyCheck | null>(null);
  const path = { params: { path: { world_id: worldId, name: connector.name }, query: version ? { version } : {} } };

  async function open() {
    setSaid(null);
    const { data, error } = await api.GET("/api/worlds/{world_id}/packages/{name}/setup", path);
    if (data) setForm(data);
    else setSaid({ ok: false, message: detail(error) ?? "Could not load its setup. Try again." });
  }

  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!form) return;
    const values = new FormData(e.currentTarget);
    const pick = (scope: string) =>
      Object.fromEntries(form.fields.filter((f) => values.has(`${scope}:${f.key}`)).map((f) => [f.key, String(values.get(`${scope}:${f.key}`)).trim()]));
    setBusy(true);
    setSaid(null);
    const { data, error } = await api.PUT("/api/worlds/{world_id}/packages/{name}/setup", { ...path, body: { shared: pick("shared"), world: pick("world") } });
    setBusy(false);
    if (error || !data) {
      setSaid({ ok: false, message: detail(error) ?? "Could not save it. Try again." });
      return;
    }
    setForm(null);
    setSaid(data.check ?? { ok: true, message: "" });
    await onSaved();
  }

  const shown = said && (
    <span role={said.ok ? "status" : "alert"} className="key-check">
      {said.ok ? `Setup saved. ${said.message}` : said.message}
    </span>
  );
  if (!form) {
    return (
      <>
        <button type="button" onClick={open} aria-label={`Set up ${connector.name}`}>
          {connector.missing.length > 0 ? "Set up" : "Change setup"}
        </button>
        {shown}
      </>
    );
  }
  return (
    <form className="set-keys setup" onSubmit={save} aria-label={`Setup of ${connector.label}`}>
      {form.fields.map((f) => {
        if (f.type === "oauth") return <SignIn key={f.key} f={f} connector={connector} worldId={worldId} />;
        const required = f.required && f.default === null && !f.shared_set && !f.world_set;
        return (
          <div key={f.key} className="setup-field">
            <label>
              <span>
                {f.label}
                {f.required ? "" : <span className="faint-title"> (optional)</span>}
                <span className="faint-title"> · {f.scope === "world" ? "this world" : "every world"}</span>
                {f.secret && <span className="faint-title"> · Credential, {f.shared_set || f.world_set ? "set" : "not set"}</span>}
              </span>
              <FieldInput f={f} scope={f.scope} required={required} />
            </label>
            {f.help && <p className="faint">{f.help}</p>}
            {f.scope === "shared" && (
              <details open={f.world_set}>
                <summary>This world only</summary>
                <label>
                  {f.label} here
                  <FieldInput f={f} scope="world" required={false} />
                </label>
              </details>
            )}
          </div>
        );
      })}
      <div className="row">
        <button type="submit" className="primary" disabled={busy}>
          {busy ? (form.test ? "Testing..." : "Saving...") : form.test ? "Test and save" : "Save"}
        </button>
        <button type="button" onClick={() => setForm(null)}>
          Cancel
        </button>
        <span className="faint">
          Stored in the data folder, mode 600, for {connector.label} only. Credentials are never shown again.
          {form.help_url && (
            <>
              {" "}
              <a href={form.help_url} target="_blank" rel="noreferrer">
                Setup help
              </a>
            </>
          )}
        </span>
      </div>
      {shown}
    </form>
  );
}

/** Leaving the app for a provider's sign-in page; a seam for tests. */
export const browser = { go: (url: string) => window.location.assign(url) };

/** An `oauth` field: Swarmify runs the sign-in, so there is no value to type, only the button
 * that sends the browser to the provider and back to this page. */
function SignIn({ f, connector, worldId }: { f: SetupField; connector: Pick<Connector, "name" | "label">; worldId: string }) {
  const [said, setSaid] = useState<string | null>(null);
  const signedIn = f.shared_set || f.world_set;
  const state = f.needs_sign_in
    ? `The provider refused the sign-in. ${connector.label} steps fail until you sign in again.`
    : signedIn
      ? "Signed in. Swarmify refreshes the access token before it expires."
      : "Not signed in.";

  async function go() {
    setSaid(null);
    const { data, error } = await api.POST("/api/worlds/{world_id}/packages/{name}/setup/oauth/{key}", {
      params: { path: { world_id: worldId, name: connector.name, key: f.key } },
    });
    if (data) browser.go(data.url);
    else setSaid(detail(error) ?? "Could not start the sign-in. Try again.");
  }

  return (
    <div className="setup-field">
      <span>
        {f.label}
        {f.required ? "" : <span className="faint-title"> (optional)</span>}
        <span className="faint-title"> · {f.scope === "world" ? "this world" : "every world"} · sign-in</span>
      </span>
      <div className="row">
        <span className={f.needs_sign_in ? "pill warning" : "faint"}>{state}</span>
        <button type="button" onClick={go} aria-label={`Sign in to ${f.label}`}>
          {f.needs_sign_in || signedIn ? "Sign in again" : "Sign in"}
        </button>
      </div>
      <p className="faint">
        Save the client ID and secret of the app you registered with the provider first. Its redirect URI is{" "}
        <code>{window.location.origin}/api/setup/oauth/callback</code>.
      </p>
      {f.help && <p className="faint">{f.help}</p>}
      {said && (
        <span role="alert" className="key-check">
          {said}
        </span>
      )}
    </div>
  );
}
