import { Link, useRouter } from "@tanstack/react-router";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";

import { useConfirm } from "../Dialog";
import { api } from "../api/client";
import { money } from "../api/runs";
import type { components } from "../api/schema";
import { settingsRoute } from "../router";
import { Loading, Skeleton } from "../Skeleton";
import { NotificationsRow } from "../Notifications";
import { InstallRow } from "../PwaNotices";
import { cap, piedPiper, setPiedPiper, the } from "../skin";
import { notify } from "../Toast";
import { ModelSelect, useModels } from "./ModelSelect";
import { Tokens } from "./Tokens";

type World = components["schemas"]["World"];
type Spend = components["schemas"]["Spend"];

type Busy = "world" | "slack" | "danger";
type Form = { monthly: string; runCap: string; retention: string; agents: string; model: string };

/** The form as the world has it: what Save compares against to show itself. */
const formOf = (w: World): Form => ({
  monthly: w.monthly_budget_usd == null ? "" : String(w.monthly_budget_usd),
  runCap: String(w.run_cap ?? 2),
  retention: String(w.retention_days ?? 90),
  agents: String(w.agent_count ?? 4),
  model: w.model ?? "",
});

/** One settings row: what it covers on the left, its panel on the right. */
function Row({ title, about, children, className = "" }: { title: string; about: string; children: ReactNode; className?: string }) {
  return (
    <section className={`set-row ${className}`.trim()} aria-label={title}>
      <div className="set-label">
        <h2>{title}</h2>
        <p className="sub">{about}</p>
      </div>
      {children}
    </section>
  );
}

export function Settings() {
  const [confirm, dialog] = useConfirm();
  const { worldId } = settingsRoute.useParams();
  const router = useRouter();
  const path = { params: { path: { world_id: worldId } } };
  const [world, setWorld] = useState<World | null>(null);
  const [spend, setSpend] = useState<Spend | null>(null);
  const [form, setForm] = useState<Form>({ monthly: "", runCap: "2", retention: "90", agents: "4", model: "" });
  const models = useModels();
  const [current, setCurrent] = useState<string | null | undefined>(undefined);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<Busy | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [skin, setSkin] = useState(piedPiper);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data }) => {
        if (!data) return;
        setWorld(data);
        setForm(formOf(data));
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

  const bad = (text: string) => notify(text, { tone: "error" });

  async function store(url: string, done: string) {
    setBusy("slack");
    const { data, error } = await api.PUT("/api/worlds/{world_id}/notifications", {
      ...path,
      body: { slack_webhook: url },
    });
    setBusy(null);
    if (error || !data) return bad(refused(error, "Not saved. Try again."));
    setCurrent(data.slack_webhook);
    setDraft("");
    notify(done, { tone: "ok" });
  }

  function saveSlack(e: FormEvent) {
    e.preventDefault();
    void store(draft, "Saved. Send a test message to check it reaches Slack.");
  }

  // One Save for the World and Runs rows, shown only once something differs from the world. The
  // rows stay disabled until the world loads, so the load cannot overwrite what was typed.
  const dirty = world !== null && JSON.stringify(form) !== JSON.stringify(formOf(world));

  async function save(e: FormEvent) {
    e.preventDefault();
    setBusy("world");
    const { data, error } = await api.PATCH("/api/worlds/{world_id}", {
      ...path,
      body: {
        monthly_budget_usd: form.monthly.trim() === "" ? null : Number(form.monthly),
        retention_days: Number(form.retention),
        model: form.model || null,
        run_cap: Number(form.runCap),
        agent_count: Number(form.agents),
      },
    });
    setBusy(null);
    if (error || !data) return bad(refused(error, "Not saved. Check the numbers and try again."));
    setWorld(data);
    setForm(formOf(data));
    setSpend((s) => (s ? { ...s, monthly_budget_usd: data.monthly_budget_usd ?? null } : s));
    notify("Saved. Applies to the next run that starts.", { tone: "ok" });
  }

  const field = (key: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [key]: e.target.value }));

  async function sendTest() {
    setBusy("slack");
    const { error } = await api.POST("/api/worlds/{world_id}/notifications/test", path);
    setBusy(null);
    if (error) {
      const detail = (error as { detail?: unknown }).detail;
      return bad(typeof detail === "string" ? `${detail}.` : "The test did not go out. Try again.");
    }
    notify("Test sent. It should be in Slack now.", { tone: "ok" });
  }

  async function remove() {
    if (!world) return;
    const ok = await confirm({
      title: `Delete ${world.name}?`,
      lines: ["Its folder, runs, library and memory go with it.", "This cannot be undone."],
      action: "Delete world",
      danger: true,
    });
    if (!ok) return;
    setBusy("danger");
    const { error } = await api.DELETE("/api/worlds/{world_id}", path);
    setBusy(null);
    if (error) return bad("Could not delete the world. Try again.");
    void router.navigate({ to: "/" });
  }

  const name = world?.name ?? "this world";
  return (
    <main>
      {dialog}
      <header className="page">
        <div>
          <h1>Settings</h1>
          {world ? (
            <p className="sub">{`${world.name} · created ${new Date(world.created_at).toLocaleDateString()}`}</p>
          ) : (
            <Loading className="sub">
              <Skeleton w="16em" h="1.5em" />
            </Loading>
          )}
        </div>
      </header>
      {loadError && <p role="alert">{loadError}</p>}
      <form className="set-form" onSubmit={save} aria-label="World settings">
        <Row title="World" about={`What ${name} may spend, how long it keeps files and the model it runs on.`}>
          <fieldset className="panel set-panel" disabled={!world}>
            <div className="settings-grid">
              <div className="field">
                <label>
                  Monthly budget, dollars
                  <input type="number" inputMode="decimal" min="0.01" step="0.01" value={form.monthly} onChange={field("monthly")} placeholder="No ceiling" />
                </label>
                <span className="faint" data-testid="spent">
                  {spend
                    ? `Spent this month: ${money(spend.spent_usd)}, an estimate${spend.monthly_budget_usd !== null ? ` of ${money(spend.monthly_budget_usd)}` : ""}.`
                    : <Skeleton w="14em" h="1.5em" />}
                </span>
                <span className="faint">
                  Each run's budget is cut to what is left of the month. Once it is spent, no run starts until next month.
                </span>
              </div>
              <div className="field">
                <label>
                  Keep videos and images for, days
                  <input type="number" inputMode="numeric" min="0" max="3650" step="1" required value={form.retention} onChange={field("retention")} />
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
          </fieldset>
        </Row>
        <Row
          title="Runs at once"
          about="How many runs can hold a slot. A run waiting at a gate keeps its slot, so runs past the cap wait in the queue until you answer one."
        >
          <fieldset className="panel set-panel" disabled={!world}>
            <div className="settings-grid">
              <div className="field">
                <label>
                  Run cap
                  <input type="number" inputMode="numeric" min="1" max="8" step="1" required value={form.runCap} onChange={field("runCap")} />
                </label>
                <span className="faint" data-testid="run-cap-help">
                  Runs waiting at a gate hold a slot too. Takes effect at once.
                </span>
              </div>
              <div className="field">
                <label>
                  Workers in the cast
                  <input type="number" inputMode="numeric" min="1" max="8" step="1" required value={form.agents} onChange={field("agents")} />
                </label>
                <span className="faint">
                  {cap(the("planner"))} and {the("judge")} always come along; this is how many workers the cast has. A swarm stage runs one unless the workflow sets its agents.{" "}
                  <Link to="/worlds/$worldId/cast" params={{ worldId }}>
                    Meet the cast
                  </Link>
                </span>
              </div>
            </div>
          </fieldset>
        </Row>
        {dirty && (
          <div className="action-bar set-savebar">
            <span className="faint">Unsaved changes</span>
            <button type="button" onClick={() => world && setForm(formOf(world))} disabled={busy === "world"}>
              Undo
            </button>
            <button type="submit" className="primary" disabled={busy === "world"}>
              {busy === "world" ? "Saving..." : "Save"}
            </button>
          </div>
        )}
      </form>
      <Row title="Slack" about="Gates can ask you in Slack too, and each message links straight to the run or the gate.">
        <form className="panel set-panel" onSubmit={saveSlack} aria-label="Slack">
          <p data-testid="slack-state">
            {current === undefined
              ? <Skeleton w="12em" h="1.5em" />
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
      <NotificationsRow Row={Row} />
      <Row title="Names" about="What the page calls the agents. Kept in this browser, for every world.">
        <div className="panel set-panel">
          <label className="check">
            <input
              type="checkbox"
              checked={skin}
              onChange={(e) => {
                setPiedPiper(e.target.checked);
                setSkin(e.target.checked);
              }}
            />
            Pied Piper names
          </label>
          <p className="faint">
            Off: Planner, Worker 1 to 4 and Judge. On: Richard plans, Gilfoyle, Dinesh, Jared and Big Head work,
            Erlich judges, Anton writes the graph and Russ keeps the budget.
          </p>
        </div>
      </Row>
      <Tokens Row={Row} />
      <InstallRow Row={Row} />
      <Row title="Danger zone" about="Deleting a world cannot be undone." className="set-danger">
        <div className="panel set-panel">
          <p>Deleting {name} removes its folder, runs, library and memory, with every file they made.</p>
          <div className="set-save start">
            <button type="button" className="danger" onClick={() => void remove()} disabled={!world || busy === "danger"}>
              Delete {name}
            </button>
          </div>
        </div>
      </Row>
    </main>
  );
}
