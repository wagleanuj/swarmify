import { Link, useNavigate, useRouter } from "@tanstack/react-router";
import { type ChangeEvent, type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import { useWorldEvents } from "../api/events";
import type { components } from "../api/schema";
import { libraryRoute } from "../router";
import { FileLink, IMAGE, Markdown, rawUrl, TypedText, VIDEO } from "./Artifacts";

type Workflow = components["schemas"]["Workflow"];
type Item = components["schemas"]["LibraryItem"];
type StageDef = components["schemas"]["WorkflowModel"]["stages"][number];
type Asset = components["schemas"]["Asset"];

const ANY = "";
type Filters = { workflow: string; stage: string; run: string; from: string; to: string };
const NONE: Filters = { workflow: ANY, stage: ANY, run: ANY, from: ANY, to: ANY };

/** The items that pass every set filter; dates compare on the calendar day. */
function filter(items: Item[], f: Filters): Item[] {
  return items.filter(
    (i) =>
      (!f.workflow || i.workflow_id === f.workflow) &&
      (!f.stage || i.stage_name === f.stage) &&
      (!f.run || i.run_id === f.run) &&
      (!f.from || i.created.slice(0, 10) >= f.from) &&
      (!f.to || i.created.slice(0, 10) <= f.to),
  );
}

const when = (ts: string) =>
  new Date(ts).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

/** Every artifact of the world across its runs (Memory's Library): type tabs, a search and a list
 * beside the preview of the item the address names, with pins, skills and runs started from it. */
export function Library() {
  const { worldId } = libraryRoute.useParams();
  const search = libraryRoute.useSearch();
  const navigate = useNavigate({ from: libraryRoute.fullPath });
  const [items, setItems] = useState<Item[] | null>(null);
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [filters, setFilters] = useState<Filters>(NONE);
  const [error, setError] = useState<string | null>(null);
  const [seen, setSeen] = useState(0);
  const [said, setSaid] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/workflows", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorkflows(data ?? []))
      .catch(() => undefined);
  }, [worldId]);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/library", { params: { path: { world_id: worldId } } })
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setItems(data);
      })
      .catch(() => setError("Could not load the library. Is the API running?"));
  }, [worldId, seen]);

  useWorldEvents(worldId, {
    "artifact.created": (created) => {
      if (created.type !== "note") setSeen((n) => n + 1);
    },
  });

  const all = items ?? [];
  const set = (key: keyof Filters) => (value: string) => setFilters((f) => ({ ...f, [key]: value }));
  const term = (search.q ?? "").toLowerCase();
  // Pinned first; the API lists the rest newest first and a stable sort keeps that.
  const shown = filter(all, filters)
    .filter((i) => (!search.type || i.type === search.type) && (!term || i.title.toLowerCase().includes(term)))
    .sort((a, b) => Number(b.pinned) - Number(a.pinned));
  const active = Object.values(filters).some(Boolean);
  const options = (pick: (i: Item) => [string, string]) =>
    [...new Map(all.map(pick)).entries()].sort((a, b) => a[1].localeCompare(b[1]));
  const types = [...new Set(all.map((i) => i.type))];
  const selected = all.find((i) => i.path === search.item);

  return (
    <main>
      <header className="page">
        <div>
          <h1>Library</h1>
          <p className="sub">
            {items
              ? `${items.length} ${items.length === 1 ? "thing" : "things"} the runs in this world made, ${items.filter((i) => i.pinned).length} pinned.`
              : "Everything this world's runs have made."}
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {said && (
        <p role="status" className="ok">
          {said}{" "}
          <Link to="/worlds/$worldId/cast" params={{ worldId }}>
            See the skills
          </Link>
        </p>
      )}
      {items === null && !error && <p role="status">Loading...</p>}
      {items?.length === 0 && (
        <div className="empty" data-testid="no-artifacts">
          <p>Nothing yet. Artifacts land here as runs finish stages, newest first.</p>
          <Link to="/worlds/$worldId/runs" params={{ worldId }} className="button">
            See runs
          </Link>
        </div>
      )}
      {items && items.length > 0 && (
        <>
          <nav className="tabs" aria-label="Types">
            {[ANY, ...types].map((t) => (
              <Link
                key={t}
                to="/worlds/$worldId/library"
                params={{ worldId }}
                search={(s) => ({ ...s, type: t || undefined })}
                activeOptions={{ exact: true }}
                className="tab"
              >
                {t ? t.replace(/_/g, " ") : "All"}{" "}
                <span className="num">{t ? all.filter((i) => i.type === t).length : all.length}</span>
              </Link>
            ))}
          </nav>
          <form className="filters" aria-label="Filters" onSubmit={(e) => e.preventDefault()}>
            <input
              type="search"
              aria-label="Search the library"
              placeholder="Search titles"
              value={search.q ?? ""}
              onChange={(e) => void navigate({ search: (s) => ({ ...s, q: e.target.value || undefined }), replace: true })}
            />
            <Select
              label="Workflow"
              value={filters.workflow}
              onChange={set("workflow")}
              options={options((i) => [i.workflow_id, workflows.find((w) => w.id === i.workflow_id)?.name ?? i.run_name])}
            />
            <Select label="Stage" value={filters.stage} onChange={set("stage")} options={options((i) => [i.stage_name, i.stage_name])} />
            <Select
              label="Run"
              value={filters.run}
              onChange={set("run")}
              options={options((i) => [i.run_id, `${i.run_name} ${i.run_id.slice(-6)}`])}
            />
            <label>
              From <input type="date" value={filters.from} onChange={(e) => set("from")(e.target.value)} />
            </label>
            <label>
              To <input type="date" value={filters.to} onChange={(e) => set("to")(e.target.value)} />
            </label>
            <span className="faint" data-testid="count">
              {shown.length} of {items.length}
            </span>
            {active && (
              <button type="button" className="link" onClick={() => setFilters(NONE)}>
                Clear filters
              </button>
            )}
          </form>
          <div className={selected ? "lib-grid has-sel" : "lib-grid"}>
            <section className="panel lib-list" aria-label="Items">
              {shown.length === 0 && <p className="faint">No artifact matches these filters.</p>}
              <ul data-testid="library">
                {shown.map((i) => (
                  <li key={i.path}>
                    <Link
                      to="/worlds/$worldId/library"
                      params={{ worldId }}
                      search={(s) => ({ ...s, item: i.path })}
                      className="lib-row"
                      data-testid="artifact"
                      data-pinned={i.pinned}
                    >
                      <span className="pill">{i.type.replace(/_/g, " ")}</span>
                      <span className="lib-title">
                        <b>
                          {i.pinned && "Pinned · "}
                          {i.title}
                        </b>
                        <span className="faint">
                          {i.run_name}
                          {i.stage_index !== null && ` · stage ${i.stage_index + 1} ${i.stage_name}`}
                          {i.workflow_version !== null && ` · v${i.workflow_version}`}
                        </span>
                      </span>
                      {i.created && (
                        <time className="faint num" dateTime={i.created}>
                          {when(i.created)}
                        </time>
                      )}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
            {selected ? (
              <Detail
                key={selected.path}
                item={selected}
                worldId={worldId}
                workflows={workflows}
                onPinned={(data) => setItems((prev) => (prev ?? []).map((i) => (i.path === data.path ? data : i)))}
                onSkill={(skill) =>
                  setSaid(`Saved ${skill.name} as version ${skill.version}. Every stage that starts from now on loads it.`)
                }
              />
            ) : (
              <section className="panel lib-detail">
                <p className="faint">
                  Choose an item to read it. Pin one and Richard and Gilfoyle read it before every later stage;
                  start a run from one to skip the stages that made it.
                </p>
              </section>
            )}
          </div>
        </>
      )}
      <Assets worldId={worldId} />
    </main>
  );
}

/** The chosen item: what it is, where it came from, its body, and what to do with it. Pinning
 * keeps you here with a stamp; starting a run names the stage it starts at. */
function Detail({
  item,
  worldId,
  workflows,
  onPinned,
  onSkill,
}: {
  item: Item;
  worldId: string;
  workflows: Workflow[];
  onPinned: (item: Item) => void;
  onSkill: (skill: Skill) => void;
}) {
  const router = useRouter();
  const [stamp, setStamp] = useState<string | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function pin() {
    setError(null);
    setStamp(null);
    const { data, error } = await api.POST("/api/worlds/{world_id}/pins", {
      params: { path: { world_id: worldId } },
      body: { path: item.path, pinned: !item.pinned },
    });
    if (error || !data) return setError(`Could not ${item.pinned ? "unpin" : "pin"} ${item.title}. Try again.`);
    onPinned(data);
    setStamp(data.pinned ? "Pinned. Later stages read it." : "Unpinned.");
  }

  return (
    <section className="panel lib-detail" aria-labelledby="item-title" data-testid="preview">
      <Link to="/worlds/$worldId/library" params={{ worldId }} search={({ item: _, ...s }) => s} className="phone-only">
        Back to the list
      </Link>
      <div className="panel-head">
        <h2 id="item-title">{item.title}</h2>
        <span className="pill">{item.type.replace(/_/g, " ")}</span>
      </div>
      <dl className="kv">
        <dt>File</dt>
        <dd className="mono">
          <FileLink a={item} worldId={worldId}>
            {item.name}
          </FileLink>
        </dd>
        <dt>From</dt>
        <dd>
          <Link to="/worlds/$worldId/runs/$runId" params={{ worldId, runId: item.run_id }}>
            {item.run_name}
          </Link>
          {item.stage_index !== null && ` · stage ${item.stage_index + 1} ${item.stage_name}`}
          {item.workflow_version !== null && ` · v${item.workflow_version}`}
        </dd>
        {item.created && (
          <>
            <dt>Made</dt>
            <dd>
              {when(item.created)}
              {item.author && ` by ${item.author}`}
            </dd>
          </>
        )}
      </dl>
      <Preview item={item} worldId={worldId} />
      <div className="actions">
        <button type="button" onClick={() => void pin()} aria-label={`${item.pinned ? "Unpin" : "Pin"} ${item.title}`}>
          {item.pinned ? "Unpin" : "Pin"}
        </button>
        <StartFrom
          item={item}
          workflows={workflows}
          onStarted={(runId) => void router.navigate({ to: "/worlds/$worldId/runs/$runId", params: { worldId, runId } })}
        />
        {item.type !== "skill_proposal" && (
          <button
            type="button"
            className="link"
            aria-expanded={drafting}
            aria-label={`Make a skill from ${item.title}`}
            onClick={() => setDrafting((d) => !d)}
          >
            Make a skill
          </button>
        )}
        {stamp && (
          <span role="status" className="faint">
            {stamp}
          </span>
        )}
      </div>
      {error && <p role="alert">{error}</p>}
      {drafting && (
        <SkillDraftForm
          item={item}
          worldId={worldId}
          onSaved={(skill) => {
            setDrafting(false);
            onSkill(skill);
          }}
        />
      )}
    </section>
  );
}

/** The item as a person reads it: a player, the image, rendered markdown, or a link. */
function Preview({ item, worldId }: { item: Item; worldId: string }) {
  const url = rawUrl(worldId, item);
  if (item.fields.deleted) return <p className="faint">{item.name} was deleted by retention; its record stays.</p>;
  if (item.type === "video" || VIDEO.test(item.name)) return <video controls preload="metadata" src={url} />;
  if (IMAGE.test(item.name)) return <img src={url} alt={item.title} />;
  if (item.name.endsWith(".md") && item.body.trim()) {
    return (
      <div className="lib-body">
        <TypedText body={item.body} />
        <Markdown text={item.body} />
      </div>
    );
  }
  return (
    <FileLink a={item} worldId={worldId}>
      Open {item.name}
    </FileLink>
  );
}

const size = (bytes: number) =>
  bytes < 1e6 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`;

/** A file name the API takes: "Calm Bed (loop).mp3" becomes "Calm-Bed-loop-.mp3". */
export const plainName = (name: string) => name.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "");

/** The world's assets folder: files the operator adds for any stage to use, never agents. */
function Assets({ worldId }: { worldId: string }) {
  const [assets, setAssets] = useState<Asset[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [seen, setSeen] = useState(0);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/assets", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setAssets(data ?? []))
      .catch(() => undefined);
  }, [worldId, seen]);

  async function upload(e: ChangeEvent<HTMLInputElement>) {
    const files = [...(e.target.files ?? [])];
    e.target.value = "";
    setBusy(true);
    setError(null);
    const failed: string[] = [];
    for (const file of files) {
      // the body is the file itself, which the typed client cannot send
      const url = `/api/worlds/${encodeURIComponent(worldId)}/assets/${encodeURIComponent(plainName(file.name))}`;
      const response = await globalThis.fetch(url, { method: "PUT", body: file }).catch(() => null);
      if (!response?.ok) failed.push(file.name);
    }
    setBusy(false);
    if (failed.length) setError(`Could not upload ${failed.join(", ")}. Try again.`);
    setSeen((n) => n + 1);
  }

  return (
    <section className="panel assets" aria-label="Assets">
      <div className="row">
        <h2>Assets</h2>
        <label className="button">
          {busy ? "Uploading..." : "Upload files"}
          <input type="file" multiple className="visually-hidden" disabled={busy} onChange={upload} aria-label="Upload files" />
        </label>
      </div>
      <p className="faint">
        Music beds, sound effects, logos: files any stage of this world can use. Agents read them and never change them.
      </p>
      {error && <p role="alert">{error}</p>}
      {assets.length === 0 ? (
        <p className="faint">No assets yet. Upload a file, or drop it into the world's assets folder.</p>
      ) : (
        <ul className="asset-list">
          {assets.map((a) => (
            <li key={a.path}>
              <a href={`/api/worlds/${worldId}/files/${a.path}`}>{a.path.replace(/^assets\//, "")}</a>
              <span className="faint">{size(a.bytes)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <label>
      {label}{" "}
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value={ANY}>All</option>
        {options.map(([v, text]) => (
          <option key={v} value={v}>
            {text}
          </option>
        ))}
      </select>
    </label>
  );
}

type Skill = components["schemas"]["Skill"];

/** A skill name from a title: lowercase, digits and hyphens, as the Agent Skills spec wants. */
export const skillName = (title: string) =>
  title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64) || "skill";

/** The SKILL.md a note or artifact becomes: its title as the description, its body as the procedure. */
export const skillDraft = (name: string, item: Item) =>
  `---\nname: ${name}\ndescription: ${item.title.replace(/\n/g, " ")}\n---\n\n${item.body.trim()}\n`;

/** Turn a note or artifact into a skill draft: edit the SKILL.md, then save it as a version. */
function SkillDraftForm({ item, worldId, onSaved }: { item: Item; worldId: string; onSaved: (skill: Skill) => void }) {
  const [name, setName] = useState(skillName(item.title));
  const [content, setContent] = useState(skillDraft(skillName(item.title), item));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function rename(next: string) {
    setContent((c) => c.replace(/^(---\nname: ).*$/m, `$1${next}`));
    setName(next);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const { data, error, response } = await api.POST("/api/worlds/{world_id}/skills", {
      params: { path: { world_id: worldId } },
      body: { name, content, run_id: item.run_id || null, note: `Promoted from ${item.path}` },
    });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      return setError(typeof detail === "string" ? detail : `Could not save the skill (${response.status}).`);
    }
    onSaved(data);
  }

  return (
    <form className="promote" aria-label={`Make a skill from ${item.title}`} onSubmit={submit}>
      <label>
        Skill name
        <input value={name} onChange={(e) => rename(e.target.value)} required pattern="[a-z0-9][a-z0-9-]*" />
      </label>
      <label>
        SKILL.md
        <textarea value={content} rows={8} onChange={(e) => setContent(e.target.value)} />
      </label>
      <span className="faint">
        Saved as a version of this world's skills, with {item.run_name || "its run"} as where it came from.
        Gilfoyle and the others load it from the next stage on.
      </span>
      <button type="submit" disabled={busy}>
        {busy ? "Saving..." : "Save skill"}
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}

type Start = { workflow: Workflow; stage: StageDef; index: number };

/** "Start a run from here" as a menu: one item per stage a new run can start at, naming the stage
 * and the workflow, with this artifact standing in for the skipped stages' output. The item's own
 * workflow comes first and starts after the stage that made it. */
function StartFrom({ item, workflows, onStarted }: { item: Item; workflows: Workflow[]; onStarted: (runId: string) => void }) {
  const [open, setOpen] = useState(false);
  const [starts, setStarts] = useState<Start[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || starts) return;
    const ordered = [...workflows].sort((a, b) => Number(b.id === item.workflow_id) - Number(a.id === item.workflow_id));
    Promise.all(
      ordered.map((w) =>
        api
          .GET("/api/workflows/{workflow_id}/versions/{number}", {
            params: { path: { workflow_id: w.id, number: w.current_version } },
          })
          .then(({ data }) => {
            const after = w.id === item.workflow_id && item.stage_index !== null ? item.stage_index + 1 : 1;
            return (data?.model.stages ?? []).flatMap((stage, index) => (index >= Math.max(1, after) ? [{ workflow: w, stage, index }] : []));
          }),
      ),
    )
      .then((lists) => setStarts(lists.flat()))
      .catch(() => setError("Could not load the workflows' stages."));
  }, [open, starts, workflows, item]);

  async function start(s: Start) {
    setBusy(true);
    setError(null);
    const { data, error, response } = await api.POST("/api/workflows/{workflow_id}/promote", {
      params: { path: { workflow_id: s.workflow.id } },
      body: { artifact: item.path, stage: s.index },
    });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      return setError(typeof detail === "string" ? detail : `Could not start the run (${response.status}).`);
    }
    onStarted(data.id);
  }

  return (
    <span className="rel" onKeyDown={(e) => e.key === "Escape" && setOpen(false)}>
      <button type="button" aria-haspopup="menu" aria-expanded={open} disabled={workflows.length === 0 || busy} onClick={() => setOpen((o) => !o)}>
        {busy ? "Starting..." : "Start a run from here"}
      </button>
      {open && (
        <span className="menu" role="menu" aria-label="Start a run from here">
          {starts === null && !error && <span className="faint">Loading stages...</span>}
          {starts?.length === 0 && <span className="faint">No later stage to start at. Start a run of the workflow instead.</span>}
          {starts?.map((s) => (
            <button key={`${s.workflow.id} ${s.index}`} type="button" role="menuitem" disabled={busy} onClick={() => void start(s)}>
              {s.stage.name}
              <small>
                Opens a new run of {s.workflow.name} at {s.stage.name}, using this {item.type.replace(/_/g, " ")}
              </small>
            </button>
          ))}
        </span>
      )}
      {error && <span role="alert">{error}</span>}
    </span>
  );
}
