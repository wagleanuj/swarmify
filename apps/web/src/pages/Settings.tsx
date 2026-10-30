import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";

import { api } from "../api/client";
import { money } from "../api/runs";
import type { components } from "../api/schema";
import { settingsRoute } from "../router";
import { ModelSelect, useModels } from "./ModelSelect";

type World = components["schemas"]["World"];
type Spend = components["schemas"]["Spend"];
type WorldSettings = components["schemas"]["WorldSettings"];

type Panel = "world" | "runs" | "slack" | "danger";
type Said = { panel: Panel; text: string; bad?: boolean };

const stamp = () => new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** One settings row: what it covers on the left, its panel on the right. */
function Row({ title, about, children }: { title: string; about: string; children: ReactNode }) {
  return (
    <section className="set-row" aria-label={title}>
      <div className="set-label">
        <h2>{title}</h2>
        <p className="sub">{about}</p>
      </div>
      {children}
    </section>
  );
}

export function Settings() {
  const { worldId } = settingsRoute.useParams();
  const router = useRouter();
  const path = { params: { path: { world_id: worldId } } };
  const [world, setWorld] = useState<World | null>(null);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [form, setForm] = useState({ monthly: "", runCap: "2", retention: "90", agents: "4", model: "" });
  const models = useModels();
  const [current, setCurrent] = useState<string | null | undefined>(undefined);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<Panel | null>(null);
  const [said, setSaid] = useState<Said | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data }) => {
        if (!data) return;
        setWorld(data);
        setForm({
          monthly: data.monthly_budget_usd == null ? "" : String(data.monthly_budget_usd),
          runCap: String(data.run_cap ?? 2),
          retention: String(data.retention_days ?? 90),
          agents: String(data.agent_count ?? 4),
          model: data.model ?? "",
        });
      })
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/spend", path)
      .then(({ data }) => setSpend(data ?? null))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/notifications", path)
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setCurrent(data.slack_webhook);
      })
      .catch(() => setLoadError("Could not load the settings. Is the API running?"));
  }, [worldId]); // eslint-disable-line react-hooks/exhaustive-deps

  const refused = (error: unknown, fallback: string) => {
    const detail = (error as { detail?: unknown } | undefined)?.detail;
    return typeof detail === "string" ? `Not saved: ${detail}.` : fallback;
  };

  async function store(url: string, done: string) {
    setBusy("slack");
    setSaid(null);
    const { data, error } = await api.PUT("/api/worlds/{world_id}/notifications", {
      ...path,
      body: { slack_webhook: url },
    });
    setBusy(null);
    if (error || !data) {
      setSaid({ panel: "slack", text: refused(error, "Not saved. Try again."), bad: true });
      return;
    }
    setCurrent(data.slack_webhook);
    setDraft("");
    setSaid({ panel: "slack", text: done });
  }

  function saveSlack(e: FormEvent) {
    e.preventDefault();
    void store(draft, "Saved. Send a test message to check it reaches Slack.");
  }

  // Each panel saves only its own settings; the rest of the form stays as typed.
  async function patch(panel: Panel, body: WorldSettings) {
    setBusy(panel);
    setSaid(null);
    const { data, error } = await api.PATCH("/api/worlds/{world_id}", { ...path, body });
    setBusy(null);
    if (error || !data) {
      setSaid({ panel, text: refused(error, "Not saved. Check the numbers and try again."), bad: true });
      return;
    }
    setWorld(data);
    setSpend((s) => (s ? { ...s, monthly_budget_usd: data.monthly_budget_usd ?? null } : s));
    setSaid({ panel, text: `Saved at ${stamp()}. Applies to the next run that starts.` });
  }

  function saveWorld(e: FormEvent) {
    e.preventDefault();
    void patch("world", {
      monthly_budget_usd: form.monthly.trim() === "" ? null : Number(form.monthly),
      retention_days: Number(form.retention),
      model: form.model || null,
    });
  }

  function saveRuns(e: FormEvent) {
    e.preventDefault();
    void patch("runs", { run_cap: Number(form.runCap), agent_count: Number(form.agents) });
  }

  const field = (key: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  async function sendTest() {
    setBusy("slack");
    setSaid(null);
    const { error } = await api.POST("/api/worlds/{world_id}/notifications/test", path);
    setBusy(null);
    if (error) {
      const detail = (error as { detail?: unknown }).detail;
      setSaid({ panel: "slack", text: typeof detail === "string" ? `${detail}.` : "The test did not go out. Try again.", bad: true });
      return;
    }
    setSaid({ panel: "slack", text: "Test sent. It should be in Slack now." });
  }

  async function remove() {
    if (!world) return;
    const ok = window.confirm(
      `Delete ${world.name}? Its folder, runs, library and memory go with it. This cannot be undone.`,
    );
    if (!ok) return;
    setBusy("danger");
    const { error } = await api.DELETE("/api/worlds/{world_id}", path);
    setBusy(null);
    if (error) {
      setSaid({ panel: "danger", text: "Could not delete the world. Try again.", bad: true });
      return;
    }
    void router.navigate({ to: "/" });
  }

  // What the panel's last save said, beside its button.
  const note = (panel: Panel) =>
    said?.panel === panel &&
    (said.bad ? (
      <p role="alert">{said.text}</p>
    ) : (
      <p role="status" className="ok">
        {said.text}
      </p>
    ));

  const name = world?.name ?? "this world";
  return (
    <main>
      <header className="page">
        <div>
          <h1>Settings</h1>
          <p className="sub">
            {world ? `${world.name} · created ${new Date(world.created_at).toLocaleDateString()}` : "Loading..."}
          </p>
        </div>
      </header>
      {loadError && <p role="alert">{loadError}</p>}
      <Row title="World" about={`What ${name} may spend, how long it keeps files and the model it runs on.`}>
        <form className="panel set-panel" onSubmit={saveWorld} aria-label="World">
          <div className="settings-grid">
            <div className="field">
              <label>
                Monthly budget, dollars
                <input type="number" min="0.01" step="0.01" value={form.monthly} onChange={field("monthly")} placeholder="No ceiling" />
              </label>
              <span className="faint" data-testid="spent">
                {spend
                  ? `Spent this month: ${money(spend.spent_usd)}, an estimate${spend.monthly_budget_usd !== null ? ` of ${money(spend.monthly_budget_usd)}` : ""}.`
                  : "Loading spend..."}
              </span>
              <span className="faint">
                Each run's budget is cut to what is left of the month. Once it is spent, no run starts until next month.
              </span>
            </div>
            <div className="field">
              <label>
                Keep videos and images for, days
                <input type="number" min="0" max="3650" step="1" required value={form.retention} onChange={field("retention")} />
              </label>
              <span className="faint">0 keeps them forever. Notes and markdown always stay.</span>
            </div>
            <div className="field">
              <ModelSelect
                label="Model"
                value={form.model}
                fallback={`Default: ${models?.default ?? "the global default"}`}
                models={models}
                onChange={(model) => setForm((f) => ({ ...f, model }))}
              />
              <span className="faint">
                What every agent and the planner chat run on. The cast can choose per role, a stage per workflow. Cheaper models cost less per step.
              </span>
            </div>
          </div>
          <div className="set-save">
            {note("world")}
            <button type="submit" disabled={busy === "world"}>
              Save
            </button>
          </div>
        </form>
      </Row>
      <Row
        title="Runs at once"
        about="How many runs can hold a slot. A run waiting at a gate keeps its slot, so runs past the cap wait in the queue until you answer one."
      >
        <form className="panel set-panel" onSubmit={saveRuns} aria-label="Runs at once">
          <div className="settings-grid">
            <div className="field">
              <label>
                Run cap
                <input type="number" min="1" max="8" step="1" required value={form.runCap} onChange={field("runCap")} />
              </label>
              <span className="faint" data-testid="run-cap-help">
                Runs waiting at a gate hold a slot too. Takes effect at once.
              </span>
            </div>
            <div className="field">
              <label>
                Workers in the cast
                <input type="number" min="1" max="8" step="1" required value={form.agents} onChange={field("agents")} />
              </label>
              <span className="faint">
                Richard and Erlich always come along; this is how many workers the cast has. A swarm stage runs one unless the workflow sets its agents.{" "}
                <Link to="/worlds/$worldId/cast" params={{ worldId }}>
                  Meet the cast
                </Link>
              </span>
            </div>
          </div>
          <div className="set-save">
            {note("runs")}
            <button type="submit" disabled={busy === "runs"}>
              Save
            </button>
          </div>
        </form>
      </Row>
      <Row title="Slack" about="Gates can ask you in Slack too, and each message links straight to the run or the gate.">
        <form className="panel set-panel" onSubmit={saveSlack} aria-label="Slack">
          <p data-testid="slack-state">
            {current === undefined
              ? "Loading..."
              : current
                ? `Posting to ${current}`
                : "Not connected. Gates wait until you open the app."}
          </p>
          <p className="faint">
            A message goes out when a gate opens, when the budget reaches 80 and 100 percent, and when a run is done or
            fails.
          </p>
          <label>
            {current ? "Replace the incoming webhook URL" : "Incoming webhook URL"}
            <input
              type="url"
              required
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="https://hooks.slack.com/services/..."
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <div className="set-save">
            {note("slack")}
            {current && (
              <>
                <button
                  type="button"
                  className="danger"
                  onClick={() => void store("", "Disconnected. Nothing goes to Slack now.")}
                  disabled={busy === "slack"}
                >
                  Disconnect
                </button>
                <button type="button" onClick={sendTest} disabled={busy === "slack"}>
                  Send a test message
                </button>
              </>
            )}
            <button type="submit" disabled={busy === "slack"}>
              Save webhook
            </button>
          </div>
        </form>
      </Row>
      <Row title="Danger" about="Deleting a world cannot be undone.">
        <div className="panel set-panel">
          <p>Deleting {name} removes its folder, runs, library and memory, with every file they made.</p>
          <div className="set-save start">
            <button type="button" className="danger" onClick={() => void remove()} disabled={!world || busy === "danger"}>
              Delete {name}
            </button>
            {note("danger")}
          </div>
        </div>
      </Row>
    </main>
  );
}
