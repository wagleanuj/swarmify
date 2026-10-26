import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { api } from "../api/client";
import type { components } from "../api/schema";
import { settingsRoute } from "../router";

type World = components["schemas"]["World"];

export function Settings() {
  const { worldId } = settingsRoute.useParams();
  const path = { params: { path: { world_id: worldId } } };
  const [world, setWorld] = useState<World | null>(null);
  const [current, setCurrent] = useState<string | null | undefined>(undefined);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}", path)
      .then(({ data }) => setWorld(data ?? null))
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
          <p className="sub">How {name} reaches you when a run needs you.</p>
        </div>
      </header>
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
