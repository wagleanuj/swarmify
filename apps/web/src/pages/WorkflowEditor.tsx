import { Link, useParams, useRouter } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";

type Violation = components["schemas"]["Violation"];
type WorkflowModel = components["schemas"]["WorkflowModel"];
type WorkflowVersion = components["schemas"]["WorkflowVersion"];
type World = components["schemas"]["World"];

export const STARTER = `name: Weekly short
budget_usd: 5
agents: 4
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week. Check the graph for what we already published.
    done_when: 5 ideas ranked, each with a hook line and a why
    tools: [web_search, graph]
    output_type: list[idea]
  - gate: pick
    ask: Pick one idea. Optional note for the script.
    kind: pick
`;

const CHECK_DELAY_MS = 300;

export function WorkflowEditor() {
  const { worldId, workflowId } = useParams({ strict: false }) as {
    worldId: string;
    workflowId?: string;
  };
  const router = useRouter();
  const [world, setWorld] = useState<World | null>(null);
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [text, setText] = useState<string | null>(workflowId ? null : STARTER);
  const [savedText, setSavedText] = useState<string | null>(null);
  const [violations, setViolations] = useState<Violation[] | null>(null);
  const [model, setModel] = useState<WorkflowModel | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const latestCheck = useRef(0);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setWorld(data ?? null))
      .catch(() => setError("Could not load this world. Is the API running?"));
  }, [worldId]);

  useEffect(() => {
    if (!workflowId) return;
    api
      .GET("/api/workflows/{workflow_id}/versions", {
        params: { path: { workflow_id: workflowId } },
      })
      .then(({ data, response }) => {
        if (response.status === 404) throw new Error("gone");
        if (!data) throw new Error("load failed");
        setVersions(data);
        const latest = data[data.length - 1];
        if (latest) {
          setText(latest.yaml);
          setSavedText(latest.yaml);
        }
      })
      .catch(() => setError("Could not load this workflow. It may have been deleted."));
  }, [workflowId]);

  // Inline validation: every rule the save would apply, run as you type, debounced.
  useEffect(() => {
    if (text === null) return;
    const id = ++latestCheck.current;
    setChecking(true);
    const timer = setTimeout(async () => {
      let data;
      try {
        ({ data } = await api.POST("/api/worlds/{world_id}/workflows/check", {
          params: { path: { world_id: worldId } },
          body: { yaml: text, workflow_id: workflowId ?? null },
        }));
      } catch {
        data = undefined;
      }
      if (id !== latestCheck.current) return;
      setChecking(false);
      if (!data) {
        setError("Could not check the workflow. Is the API running?");
        return;
      }
      setError(null);
      setViolations(data.violations);
      setModel(data.model ?? null);
    }, CHECK_DELAY_MS);
    return () => clearTimeout(timer);
  }, [text, worldId, workflowId]);

  const nextVersion = (versions[versions.length - 1]?.number ?? 0) + 1;
  const dirty = text !== savedText;
  const clean = violations !== null && violations.length === 0;
  const canSave = text !== null && dirty && clean && !checking && !busy;

  async function save() {
    if (text === null) return;
    setBusy(true);
    setNotice(null);
    const result = workflowId
      ? await api.POST("/api/workflows/{workflow_id}/versions", {
          params: { path: { workflow_id: workflowId } },
          body: { yaml: text },
        })
      : await api.POST("/api/worlds/{world_id}/workflows", {
          params: { path: { world_id: worldId } },
          body: { yaml: text },
        });
    setBusy(false);
    if (result.error || !result.data) {
      const refused = result.error as { violations?: Violation[] } | undefined;
      if (refused?.violations) setViolations(refused.violations);
      else setError("Could not save the workflow. Try again.");
      return;
    }
    const version = result.data;
    setSavedText(text);
    setNotice(`Saved as version ${version.number}.`);
    if (!workflowId) {
      void router.navigate({
        to: "/worlds/$worldId/workflows/$workflowId",
        params: { worldId, workflowId: version.workflow_id },
      });
      return;
    }
    setVersions((prev) => [...prev, version]);
  }

  const title = model?.name?.trim() || (workflowId ? "Workflow" : "New workflow");

  return (
    <main className="editor-page">
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> /{" "}
            <Link to="/worlds/$worldId" params={{ worldId }}>
              {world?.name.toLowerCase() ?? "world"}
            </Link>{" "}
            / workflows
          </div>
          <h1>{title}</h1>
          <p className="sub">
            Four stage types: swarm, gate, connector, workflow. Every save is a new version and
            runs keep the version they started with, so editing never changes a run in flight.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      <div className="editor">
        <section aria-label="Editor">
          <label htmlFor="yaml">Workflow YAML</label>
          <textarea
              id="yaml"
              name="yaml"
              value={text ?? ""}
              disabled={text === null}
              spellCheck={false}
              rows={22}
              onChange={(e) => {
                setNotice(null);
                setText(e.target.value);
              }}
          />
          <div className="editor-actions">
            <button type="button" className="primary" disabled={!canSave} onClick={save}>
              {busy ? "Saving..." : `Save as version ${nextVersion}`}
            </button>
            <span className="faint" role="status" data-testid="check-status">
              {checking
                ? "Checking..."
                : notice ?? (dirty ? "Unsaved changes." : "Everything here is saved.")}
            </span>
          </div>
          <Problems violations={violations} checking={checking} />
        </section>
        <aside>
          <section aria-label="Stages">
            <h2>Stages</h2>
            {model && model.stages.length > 0 ? (
              <ol className="stage-rail" data-testid="stage-rail">
                {model.stages.map((s, i) => (
                  <li key={i}>
                    <b>{s.name}</b> <span className="faint">{s.type}</span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="faint">The stage list appears as soon as the YAML parses.</p>
            )}
          </section>
          <section aria-label="Versions">
            <h2>Versions</h2>
            {workflowId ? (
              <ul className="versions" data-testid="versions">
                {[...versions].reverse().map((v) => (
                  <li key={v.id}>
                    <span>
                      <b>v{v.number}</b>{" "}
                      <time dateTime={v.created_at} className="faint">
                        {new Date(v.created_at).toLocaleString()}
                      </time>
                    </span>
                    {v.yaml === text ? (
                      <span className="faint">open</span>
                    ) : (
                      <button type="button" onClick={() => setText(v.yaml)}>
                        Open v{v.number}
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="faint">Version 1 is created when you save.</p>
            )}
          </section>
        </aside>
      </div>
    </main>
  );
}

function Problems({ violations, checking }: { violations: Violation[] | null; checking: boolean }) {
  if (violations === null) return null;
  if (violations.length === 0) {
    return (
      <p className="ok" data-testid="problems">
        No problems. Every rule passes{checking ? ", rechecking" : ""}.
      </p>
    );
  }
  return (
    <section aria-label="Problems" data-testid="problems">
      <h2>
        {violations.length} {violations.length === 1 ? "problem" : "problems"} to fix before saving
      </h2>
      <ul className="problems">
        {violations.map((v, i) => (
          <li key={i}>
            {v.stage && <b>{v.stage}: </b>}
            {v.message} <code>{v.rule}</code>
          </li>
        ))}
      </ul>
    </section>
  );
}
