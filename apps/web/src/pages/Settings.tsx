import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import { money } from "../api/runs";
import type { components } from "../api/schema";
import { settingsRoute } from "../router";
import { ModelSelect, useModels } from "./ModelSelect";

type World = components["schemas"]["World"];
type Spend = components["schemas"]["Spend"];

export function Settings() {
  const { worldId } = settingsRoute.useParams();
  const path = { params: { path: { world_id: worldId } } };
  const [world, setWorld] = useState<World | null>(null);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [form, setForm] = useState({ monthly: "", runCap: "2", retention: "90", agents: "4", model: "" });
  const models = useModels();
  const [savingWorld, setSavingWorld] = useState(false);
  const [worldSaid, setWorldSaid] = useState<string | null>(null);
  const [worldError, setWorldError] = useState<string | null>(null);
  const [current, setCurrent] = useState<string | null | undefined>(undefined);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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
      .catch(() => setError("Could not load the settings. Is the API running?"));
  }, [worldId]); // eslint-disable-line react-hooks/exhaustive-deps

  async function store(url: string, done: string) {
    setBusy(true);
    setError(null);
    setSaid(null);
    const { data, error } = await api.PUT("/api/worlds/{world_id}/notifications", {
      ...path,
      body: { slack_webhook: url },
    });
    setBusy(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setError(typeof detail === "string" ? `Not saved: ${detail}.` : "Not saved. Try again.");
      return;
    }
    setCurrent(data.slack_webhook);
    setDraft("");
    setSaid(done);
  }

  function save(e: FormEvent) {
    e.preventDefault();
    void store(draft, "Saved. Send a test message to check it reaches Slack.");
  }

  async function saveWorld(e: FormEvent) {
    e.preventDefault();
    setSavingWorld(true);
    setWorldError(null);
    setWorldSaid(null);
    const { data, error } = await api.PATCH("/api/worlds/{world_id}", {
      ...path,
      body: {
        monthly_budget_usd: form.monthly.trim() === "" ? null : Number(form.monthly),
        run_cap: Number(form.runCap),
        retention_days: Number(form.retention),
        agent_count: Number(form.agents),
        model: form.model || null,
      },
    });
    setSavingWorld(false);
    if (error || !data) {
      const detail = (error as { detail?: unknown } | undefined)?.detail;
      setWorldError(typeof detail === "string" ? `Not saved: ${detail}.` : "Not saved. Check the numbers and try again.");
      return;
    }
    setWorld(data);
    setSpend((s) => (s ? { ...s, monthly_budget_usd: data.monthly_budget_usd ?? null } : s));
    setWorldSaid("Settings saved. They apply to the next run that starts.");
  }

  const field = (key: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  async function sendTest() {
    setBusy(true);
    setError(null);
    setSaid(null);
    const { error } = await api.POST("/api/worlds/{world_id}/notifications/test", path);
    setBusy(false);
    if (error) {
      const detail = (error as { detail?: unknown }).detail;
      setError(typeof detail === "string" ? `${detail}.` : "The test did not go out. Try again.");
      return;
    }
    setSaid("Test sent. It should be in Slack now.");
  }

  const name = world?.name ?? "this world";
  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> /{" "}
            <Link to="/worlds/$worldId" params={{ worldId }}>
              {name.toLowerCase()}
            </Link>{" "}
            / settings
          </div>
          <h1>Settings</h1>
          <p className="sub">What {name} may spend and run, how long it keeps files, and how it reaches you.</p>
        </div>
      </header>
      {worldError && <p role="alert">{worldError}</p>}
      {worldSaid && (
        <p role="status" className="ok">
          {worldSaid}
        </p>
      )}
      <form className="card" onSubmit={saveWorld} aria-label="World">
        <h2>World</h2>
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
              Runs at once
              <input type="number" min="1" max="8" step="1" required value={form.runCap} onChange={field("runCap")} />
            </label>
            <span className="faint">Runs past the cap wait in the queue. Takes effect at once.</span>
          </div>
          <div className="field">
            <label>
              Keep videos and images for, days
              <input type="number" min="0" max="3650" step="1" required value={form.retention} onChange={field("retention")} />
            </label>
            <span className="faint">0 keeps them forever. Notes and markdown always stay.</span>
          </div>
          <div className="field">
            <label>
              Workers per stage
              <input type="number" min="1" max="8" step="1" required value={form.agents} onChange={field("agents")} />
            </label>
            <span className="faint">
              Richard and Erlich always come along; this is how many workers the cast has. A stage starts one per ready task, up to this.
            </span>
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
        <div className="row">
          <button type="submit" className="primary" disabled={savingWorld}>
            Save settings
          </button>
          <Link to="/worlds/$worldId/cast" params={{ worldId }}>
            Meet the cast
          </Link>
        </div>
      </form>
      {error && <p role="alert">{error}</p>}
      {said && (
        <p role="status" className="ok">
          {said}
        </p>
      )}
      <form className="card" onSubmit={save} aria-label="Slack">
        <h2>Slack</h2>
        <p data-testid="slack-state">
          {current === undefined
            ? "Loading..."
            : current
              ? `Posting to ${current}`
              : "Not connected. Gates wait until you open the app."}
        </p>
        <p className="faint">
          A message goes out when a gate opens, when the budget reaches 80 and 100 percent, and
          when a run is done or fails. Each one links straight to the run or the gate.
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
        <div className="row">
          <button type="submit" className="primary" disabled={busy}>
            Save webhook
          </button>
          {current && (
            <>
              <button type="button" onClick={sendTest} disabled={busy}>
                Send a test message
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => void store("", "Disconnected. Nothing goes to Slack now.")}
                disabled={busy}
              >
                Disconnect
              </button>
            </>
          )}
        </div>
      </form>
    </main>
  );
}
