import { Link } from "@tanstack/react-router";
import { type FormEvent, useCallback, useEffect, useId, useState } from "react";

import { api } from "../api/client";
import type { ArtifactFile } from "../api/events";
import { actOn, decide, type Gate, money, setBudget } from "../api/runs";
import type { components } from "../api/schema";
import { ArtifactCards, ArtifactView, choosable, underReview } from "./Artifacts";
import { RunRef } from "./Runs";
import { LABEL } from "./Connectors";

type World = components["schemas"]["World"];
type PublishPreview = components["schemas"]["PublishPreview"];
type Proposal = components["schemas"]["Proposal"];

export function Gates() {
  const [gates, setGates] = useState<Gate[] | null>(null);
  const [worlds, setWorlds] = useState<World[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .GET("/api/gates")
      .then(({ data, error }) => {
        if (error || !data) throw new Error("load failed");
        setGates(data);
      })
      .catch(() => setError("Could not load the gates. Is the API running?"));
  }, []);

  useEffect(load, [load]);
  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
      .catch(() => undefined);
  }, []);

  // Gates live across worlds, so follow the one all-worlds stream and refetch the inbox
  // whenever a run moves (ADR 0002: skins render events, never tables). A stream per world
  // held all six of the browser's connections per host and queued every answer behind them.
  // `since=now` skips history: replaying it refetched the inbox once per past run.updated.
  useEffect(() => {
    const source = new EventSource("/api/events?since=now");
    source.addEventListener("run.updated", load);
    return () => source.close();
  }, [load]);

  async function answer(gate: Gate, send: () => Promise<string | null>) {
    setError(null);
    const refused = await send();
    if (refused) setError(refused);
    // A budget gate stays until the run resumes; it can still be aborted, so refetch.
    if (refused || gate.kind === "budget") {
      load();
    } else {
      // Hide the answered card now; the run's next run.updated refetches the inbox. Refetching
      // here could beat the workflow to the message and show the answered gate again.
      setGates((gs) => gs?.filter((g) => g.stage_id !== gate.stage_id) ?? gs);
    }
  }

  const worldName = (id: string) => worlds.find((w) => w.id === id)?.name ?? id;

  return (
    <main>
      <header className="page">
        <div>
          <div className="crumb">
            <Link to="/">worlds</Link> / needs you
          </div>
          <h1>Needs you</h1>
          <p className="sub">
            Every open gate from every world and run, newest first. Answer here and the run moves
            on; your answer is kept as a decision file in the run folder.
          </p>
        </div>
      </header>
      {error && <p role="alert">{error}</p>}
      {gates === null && !error && <p role="status">Loading...</p>}
      {gates?.length === 0 && (
        <div className="empty" data-testid="no-gates">
          <p>
            Nothing is waiting on you. When a run reaches a gate it lands here, pausing until you
            pick, approve or confirm, and so does a run whose budget ran out.
          </p>
          <Link to="/" className="button">
            See your worlds
          </Link>
        </div>
      )}
      {gates && gates.length > 0 && (
        <ul className="gate-list" data-testid="gate-list">
          {gates.map((g) => (
            <GateCard key={g.stage_id} gate={g} world={worldName(g.world_id)} onAnswer={answer} />
          ))}
        </ul>
      )}
    </main>
  );
}

function GateCard({
  gate,
  world,
  onAnswer,
}: {
  gate: Gate;
  world: string;
  onAnswer: (gate: Gate, send: () => Promise<string | null>) => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [sendBack, setSendBack] = useState(false);
  // What the earlier stages made: the cards a pick chooses from, the piece an approve reviews
  // (stories 53 and 54). null until loaded, so the pick's fallback field does not flash.
  const [artifacts, setArtifacts] = useState<ArtifactFile[] | null>(null);
  const shows = gate.kind === "pick" || gate.kind === "approve";

  useEffect(() => {
    if (!shows) return;
    api
      .GET("/api/runs/{run_id}/artifacts", {
        params: { path: { run_id: gate.run_id }, query: { before: gate.stage_index } },
      })
      .then(({ data }) => setArtifacts(data ?? []))
      .catch(() => setArtifacts([]));
  }, [shows, gate.run_id, gate.stage_index, gate.send_backs_left]);

  const cards = gate.kind === "pick" && artifacts ? choosable(artifacts) : [];
  const reviewed = gate.kind === "approve" && artifacts ? underReview(artifacts) : [];

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fields = new FormData(e.currentTarget);
    if (gate.kind === "budget") {
      const total = (gate.budget_usd ?? 0) + Number(fields.get("add"));
      setBusy(true);
      await onAnswer(gate, () => setBudget(gate.run_id, Math.round(total * 100) / 100));
      setBusy(false);
      return;
    }
    const note = String(fields.get("note") ?? "").trim();
    const body: Parameters<typeof decide>[1] = { note };
    if (gate.kind === "pick") {
      const picked = cards.find((c) => c.path === fields.get("artifact"));
      body.choice = picked ? picked.title : String(fields.get("choice") ?? "").trim();
      if (picked) body.artifact = picked.path;
    }
    // An approval or send-back of one piece names it, so the record says which.
    if (gate.kind === "approve" && reviewed.length === 1) body.artifact = reviewed[0]!.path;
    if (sendBack) body.send_back_to = String(fields.get("send_back_to") ?? "");
    // What goes out is what the form holds, edited or not; the decision records it.
    if (gate.publish && !sendBack) {
      body.title = String(fields.get("title") ?? "");
      body.description = String(fields.get("description") ?? "");
      // The schedule shown; a relative one that moved since is refused and shown again.
      body.schedule = gate.publish.schedule ?? null;
    }
    setBusy(true);
    await onAnswer(gate, () => decide(gate.run_id, body));
    setBusy(false);
    setSendBack(false);
  }

  async function abort() {
    if (!window.confirm(`Abort ${gate.run_name}? Finished stages keep their artifacts.`)) return;
    setBusy(true);
    await onAnswer(gate, () => actOn(gate.run_id, "abort"));
    setBusy(false);
  }

  // A post that cannot go out would fail the run; say why instead of letting it.
  const blocked = (gate.publish?.problems ?? []).length > 0;
  const since = new Date(gate.opened_at).toLocaleTimeString([], { timeStyle: "short" });
  return (
    <li className="gate" data-kind={gate.kind} data-testid={`gate-${gate.run_id}`}>
      <div className="gate-head">
        <Link
          to="/worlds/$worldId/runs/$runId"
          params={{ worldId: gate.world_id, runId: gate.run_id }}
        >
          {gate.run_name}
        </Link>
        <span className="faint">
          {world} · stage {gate.stage_index + 1} · {gate.stage_name} · waiting since {since}
        </span>
        {gate.parent && <RunRef label="child of" run={gate.parent} testId="child-of" />}
      </div>
      <p className="ask">{gate.ask}</p>
      {reviewed.length > 0 && <ArtifactView artifacts={reviewed} worldId={gate.world_id} />}
      {gate.kind === "approve" && artifacts && reviewed.length === 0 && (
        <p className="faint">The earlier stages left nothing to show here; their notes are on the run's timeline.</p>
      )}
      {gate.kind === "skill" ? (
        <SkillGate gate={gate} onAnswer={onAnswer} />
      ) : (
      <form onSubmit={submit} className="gate-form" aria-label={`Answer ${gate.stage_name}`}>
        {gate.publish && <WhatGoesOut preview={gate.publish} />}
        {gate.kind === "budget" && (
          <>
            <label>
              Add (US dollars)
              <input
                name="add"
                type="number"
                inputMode="decimal"
                min="0.01"
                step="0.01"
                required
                defaultValue={(gate.budget_usd ?? 1).toFixed(2)}
              />
            </label>
            <span className="faint">
              Russ has spent {money(gate.spent_usd ?? 0)} of {money(gate.budget_usd ?? 0)}. The
              stopped agents resume where they stopped.
            </span>
            <div className="row gate-actions">
              <button type="submit" className="primary" disabled={busy}>
                Add money and go on
              </button>
              <button type="button" className="danger" onClick={abort} disabled={busy}>
                Abort the run
              </button>
            </div>
          </>
        )}
        {gate.kind === "pick" && cards.length > 0 && <ArtifactCards cards={cards} worldId={gate.world_id} />}
        {gate.kind === "pick" && artifacts && cards.length === 0 && (
          <>
            <label>
              Your pick
              <input name="choice" required autoComplete="off" placeholder="idea 1" />
            </label>
            <span className="faint">
              No cards reached this gate, so name your pick; it is kept word for word.
            </span>
          </>
        )}
        {gate.kind !== "budget" && (
        <label>
          {gate.kind === "approve" ? "Note (sent back with it, or kept with the approval)" : "Note (optional)"}
          <textarea name="note" rows={2} />
        </label>
        )}
        {gate.kind === "approve" && sendBack && (
          <label>
            Send back to
            <select name="send_back_to" defaultValue={gate.earlier_stages.at(-1)}>
              {gate.earlier_stages.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        )}
        <div className="row gate-actions">
          {gate.kind === "pick" && (
            <>
              <button type="submit" className="primary" disabled={busy || artifacts === null}>
                Continue with this pick
              </button>
              {cards.length > 0 && (
                <span className="faint">The ones you pass on stay in the library.</span>
              )}
            </>
          )}
          {gate.kind === "confirm" && (
            <>
              <button type="submit" className="primary" disabled={busy || blocked}>
                {gate.publish ? "Confirm and post" : "Confirm"}
              </button>
              <span className="faint">
                {gate.publish
                  ? "Posts exactly what is above. Nothing goes out until you confirm."
                  : "Nothing goes out until you confirm."}
              </span>
            </>
          )}
          {gate.kind === "approve" && !sendBack && (
            <>
              <button type="submit" className="primary" disabled={busy || blocked}>
                Approve
              </button>
              {gate.send_backs_left > 0 && gate.earlier_stages.length > 0 ? (
                <>
                  <button type="button" onClick={() => setSendBack(true)} disabled={busy}>
                    Send back...
                  </button>
                  <span className="faint">{gate.send_backs_left} of 2 send-backs left</span>
                </>
              ) : (
                <span className="faint">
                  Sent back twice already. Approve, or abort the run from its timeline.
                </span>
              )}
            </>
          )}
          {gate.kind === "approve" && sendBack && (
            <>
              <button type="submit" className="primary" disabled={busy}>
                Send back
              </button>
              <button type="button" onClick={() => setSendBack(false)} disabled={busy}>
                Cancel
              </button>
              <span className="faint">The stage runs again with your note in its context.</span>
            </>
          )}
        </div>
      </form>
      )}
    </li>
  );
}

/** The retro's proposal: at most one skill change to approve as is, edited, or reject, and the
 * workflow change it drew from a recurring intervention (ADR 0008). */
function SkillGate({
  gate,
  onAnswer,
}: {
  gate: Gate;
  onAnswer: (gate: Gate, send: () => Promise<string | null>) => Promise<void>;
}) {
  const [proposal, setProposal] = useState<Proposal | null | undefined>(undefined);
  const [content, setContent] = useState("");
  const [applyWorkflow, setApplyWorkflow] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .GET("/api/runs/{run_id}/proposal", { params: { path: { run_id: gate.run_id } } })
      .then(({ data }) => {
        setProposal(data ?? null);
        setContent(data?.skill?.content ?? "");
      })
      .catch(() => setProposal(null));
  }, [gate.run_id]);

  async function decide(e: FormEvent<HTMLFormElement>, approve: boolean) {
    e.preventDefault();
    if (!proposal) return;
    const note = String(new FormData(e.currentTarget).get("note") ?? "").trim();
    if (!approve && !note && !window.confirm("Reject without saying why? The retro learns from the reason.")) return;
    setBusy(true);
    await onAnswer(gate, async () => {
      const { error, response } = await api.POST("/api/proposals/{proposal_id}/decide", {
        params: { path: { proposal_id: proposal.id } },
        body: {
          approve,
          content: proposal.skill && proposal.skill.action !== "retired" ? content : null,
          apply_workflow: approve && applyWorkflow && proposal.workflow !== null,
          note,
        },
      });
      if (!error) return null;
      const detail = (error as { detail?: unknown }).detail;
      return typeof detail === "string" ? detail : `The decision was refused (${response.status}).`;
    });
    setBusy(false);
  }

  if (proposal === undefined) return <p role="status">Loading the proposal...</p>;
  if (proposal === null) {
    return <p className="faint">The proposal could not be loaded; the run keeps waiting.</p>;
  }
  const skill = proposal.skill;
  return (
    <form className="gate-form proposal" aria-label={`Answer ${gate.stage_name}`} onSubmit={(e) => void decide(e, true)}>
      {skill && (
        <section data-testid="skill-change">
          <b>
            {skill.action === "new" && `New skill ${skill.name}`}
            {skill.action === "changed" && `Change skill ${skill.name}`}
            {skill.action === "retired" && `Retire skill ${skill.name}`}
          </b>
          <p>{skill.summary}</p>
          <span className="faint">Evidence</span>
          <ul>
            {(skill.evidence ?? []).map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
          {skill.action !== "retired" && (
            <label>
              SKILL.md (edit before approving)
              <textarea value={content} rows={8} onChange={(e) => setContent(e.target.value)} />
            </label>
          )}
        </section>
      )}
      {proposal.workflow && (
        <section data-testid="workflow-change">
          <b>Workflow change: {proposal.workflow.summary}</b>
          <ul>
            {(proposal.workflow.evidence ?? []).map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
          <pre>{proposal.workflow.yaml}</pre>
          <label className="check">
            <input type="checkbox" checked={applyWorkflow} onChange={(e) => setApplyWorkflow(e.target.checked)} />
            Save it as the workflow's next version when approving
          </label>
        </section>
      )}
      <label>
        Note (kept with the decision; on a rejection, say why)
        <textarea name="note" rows={2} />
      </label>
      <div className="row gate-actions">
        <button type="submit" className="primary" disabled={busy}>
          Approve
        </button>
        <RejectButton busy={busy} onReject={(e) => void decide(e, false)} />
        <span className="faint">
          {skill ? "Approving writes the skill as a new version every later stage loads." : "Nothing changes until you approve."}
        </span>
      </div>
    </form>
  );
}

/** A reject button that submits the surrounding form with the rejection intent. */
function RejectButton({ busy, onReject }: { busy: boolean; onReject: (e: FormEvent<HTMLFormElement>) => void }) {
  return (
    <button
      type="button"
      className="danger"
      disabled={busy}
      onClick={(e) => {
        const form = (e.currentTarget as HTMLButtonElement).form;
        if (!form) return;
        onReject({ preventDefault: () => undefined, currentTarget: form } as unknown as FormEvent<HTMLFormElement>);
      }}
    >
      Reject
    </button>
  );
}

/** What the publish stage after this gate sends out: exactly these values (ADR 0007). Title and
 * description are fields of the gate's form, so the person can edit them before confirming. */
function WhatGoesOut({ preview }: { preview: PublishPreview }) {
  const id = useId();
  const where = LABEL[preview.connector] ?? preview.connector;
  const platforms = preview.platforms?.length ? ` to ${preview.platforms.join(", ")}` : "";
  const problems = preview.problems ?? [];
  return (
    <section className="publish-preview" aria-label="What goes out">
      <dl>
        <dt>
          <label htmlFor={`${id}-title`}>Title</label>
        </dt>
        <dd>
          <input id={`${id}-title`} name="title" required maxLength={100} defaultValue={preview.title} />
        </dd>
        <dt>
          <label htmlFor={`${id}-description`}>Description</label>
        </dt>
        <dd>
          <textarea id={`${id}-description`} name="description" rows={3} maxLength={5000} defaultValue={preview.description ?? ""} />
        </dd>
        <dt>Where</dt>
        <dd>
          {where}
          {platforms}
        </dd>
        <dt>Visibility</dt>
        <dd>{preview.visibility}</dd>
        <dt>Made for kids</dt>
        <dd>{preview.made_for_kids ? "Yes" : "No"}</dd>
        <dt>When</dt>
        <dd>
          {preview.schedule ? (
            <>
              <time dateTime={preview.schedule}>
                {new Date(preview.schedule).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}
              </time>{" "}
              <span className="faint">
                ({preview.schedule.slice(0, 16).replace("T", " ")} UTC).{" "}
                {preview.connector === "youtube"
                  ? `Private until then, then ${preview.visibility}.`
                  : "Nothing posts before then."}
              </span>
            </>
          ) : (
            "Now, as soon as you confirm"
          )}
        </dd>
        {preview.video && (
          <>
            <dt>Video</dt>
            <dd className="mono">{preview.video}</dd>
          </>
        )}
      </dl>
      {problems.length > 0 && (
        <div className="publish-problems" role="alert">
          <p>This will not post:</p>
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          <p>Abort the run from its timeline, fix the publish stage, and start a new run.</p>
        </div>
      )}
    </section>
  );
}
