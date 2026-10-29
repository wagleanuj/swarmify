import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useEffect, useId, useState } from "react";

import { api } from "../api/client";
import { dropGate, reloadGates, useOpenGates } from "../api/gates";
import type { ArtifactFile } from "../api/events";
import { actOn, decide, type Gate, money, type Run, setBudget, STATUS_LABEL, took } from "../api/runs";
import type { components } from "../api/schema";
import { gatesRoute } from "../router";
import { ArtifactCards, ArtifactView, choosable, underReview } from "./Artifacts";
import { RunRef } from "./Runs";
import { LABEL } from "./Connectors";

type World = components["schemas"]["World"];
type PublishPreview = components["schemas"]["PublishPreview"];
type Proposal = components["schemas"]["Proposal"];

/** What `GateCard` hands back with an answer: `said` is the decision in plain words, for the
 * line that confirms it. */
export type Answer = (gate: Gate, send: () => Promise<string | null>, said?: string) => Promise<void>;

/** The kind of decision, as the queue names it. */
const KIND: Record<Gate["kind"], string> = {
  pick: "Pick an idea",
  approve: "Review and approve",
  confirm: "Confirm before it goes out",
  budget: "Budget ran out",
  skill: "Review a skill change",
};

const ago = (at: number) => `${took(Math.max(0, (Date.now() - at) / 1000))} ago`;

type Answered = { gate: Gate; said: string; at: string };

export function Gates() {
  const search = gatesRoute.useSearch();
  const navigate = useNavigate();
  const open = useOpenGates();
  const [worlds, setWorlds] = useState<World[]>([]);
  const [refused, setRefused] = useState<string | null>(null);
  // Answered this visit, newest first: they sort last in the queue, dimmed.
  const [answered, setAnswered] = useState<Answered[]>([]);
  const [moving, setMoving] = useState<Run[] | null>(null);
  const error = refused ?? (open.failed ? "Could not load the gates. Is the API running?" : null);

  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
      .catch(() => undefined);
  }, []);

  const inWorld = (g: Gate) => !search.world || g.world_id === search.world;
  // A gate opened again after a send back is a new decision, so match the opening too.
  const isAnswered = (g: Gate) => answered.some((a) => a.gate.stage_id === g.stage_id && a.gate.opened_at === g.opened_at);
  const waiting = (open.gates ?? []).filter((g) => inWorld(g) && !isAnswered(g));
  const done = answered.filter((a) => inWorld(a.gate));
  const selected = waiting.find((g) => g.stage_id === search.gate) ?? waiting[0];
  const last = search.done ? answered.find((a) => a.gate.stage_id === search.done) : undefined;
  const clear = open.gates !== null && waiting.length === 0;

  // An empty queue still leads somewhere: the runs moving in the worlds it covers.
  useEffect(() => {
    if (!clear) return;
    const ids = worlds.map((w) => w.id).filter((id) => !search.world || id === search.world);
    Promise.all(
      ids.map((id) => api.GET("/api/worlds/{world_id}/runs", { params: { path: { world_id: id } } }).then(({ data }) => data ?? [])),
    )
      .then((all) => setMoving(all.flat().filter((r) => r.status === "running" || r.status === "queued")))
      .catch(() => setMoving([]));
  }, [clear, worlds, search.world]);

  const answer: Answer = async (gate, send, said = "You answered it") => {
    setRefused(null);
    const why = await send();
    // A budget gate stays until the run resumes and can still be aborted, so it stays open.
    if (why || gate.kind === "budget") {
      if (why) setRefused(why);
      reloadGates();
      return;
    }
    dropGate(gate.stage_id);
    setAnswered((a) => [{ gate, said, at: new Date().toISOString() }, ...a]);
    const next = waiting.find((g) => g.stage_id !== gate.stage_id);
    void navigate({ to: "/gates", search: { world: search.world, gate: next?.stage_id, done: gate.stage_id } });
  };

  const worldName = (id: string) => worlds.find((w) => w.id === id)?.name ?? id;
  const sub = waiting.length
    ? `${waiting.length === 1 ? "One decision" : `${waiting.length} decisions`} waiting, oldest ${ago(
        Math.min(...waiting.map((g) => Date.parse(g.opened_at))),
      )}. Answering one opens the next.`
    : "Every open gate from every world lands here.";

  return (
    <main>
      <header className="page">
        <div>
          <h1>Needs you</h1>
          <p className="sub">{sub}</p>
        </div>
        <label>
          World
          <select
            value={search.world ?? ""}
            onChange={(e) => void navigate({ to: "/gates", search: e.target.value ? { world: e.target.value } : {} })}
          >
            <option value="">All worlds</option>
            {worlds.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </label>
      </header>
      {error && <p role="alert">{error}</p>}
      {open.gates === null && !error && <p role="status">Loading...</p>}
      {open.gates !== null && (
        <div className="inbox">
          <nav className="queue" aria-label="Waiting decisions" data-testid="queue">
            {waiting.map((g) => (
              <Link
                key={g.stage_id}
                className="q-item"
                to="/gates"
                search={{ world: search.world, gate: g.stage_id }}
                aria-current={g === selected ? "true" : undefined}
              >
                <span className="meta">
                  <span>{worldName(g.world_id)}</span>
                  <span>{ago(Date.parse(g.opened_at))}</span>
                </span>
                <b>{KIND[g.kind]}</b>
                <span className="muted">{g.run_name}</span>
              </Link>
            ))}
            {done.map((a) => (
              <Link
                key={`${a.gate.stage_id}-${a.at}`}
                className="q-item done"
                to="/worlds/$worldId/runs/$runId"
                params={{ worldId: a.gate.world_id, runId: a.gate.run_id }}
              >
                <span className="meta">
                  <span>{worldName(a.gate.world_id)}</span>
                  <span>Answered</span>
                </span>
                <b>{KIND[a.gate.kind]}</b>
                <span className="muted">{a.gate.run_name}</span>
              </Link>
            ))}
            {waiting.length === 0 && done.length === 0 && <p className="faint">No decisions here.</p>}
          </nav>
          <div className="inbox-main">
            {last && (
              <p className="notice" role="status" data-testid="answered">
                <span>
                  {last.said} at {new Date(last.at).toLocaleTimeString([], { timeStyle: "short" })}.
                </span>
                <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: last.gate.world_id, runId: last.gate.run_id }}>
                  Open that run
                </Link>
              </p>
            )}
            {selected ? (
              <ul className="gate-list">
                <GateCard key={selected.stage_id} gate={selected} world={worldName(selected.world_id)} onAnswer={answer} />
              </ul>
            ) : (
              <section className="empty" data-testid="no-gates">
                <h2>Nothing is waiting on you</h2>
                <p className="faint">
                  When a run reaches a gate it lands here, pausing until you pick, approve or confirm, and so does a
                  run whose budget ran out.
                </p>
                <h3>Runs still moving</h3>
                {moving === null ? (
                  <p role="status">Loading...</p>
                ) : moving.length === 0 ? (
                  <p className="faint">No runs are moving.</p>
                ) : (
                  <ul className="moving" data-testid="moving">
                    {moving.map((r) => (
                      <li key={r.id}>
                        <span className={r.status === "running" ? "dot live" : "dot"} />
                        <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: r.world_id, runId: r.id }}>
                          {r.name}
                        </Link>
                        <span className="faint">
                          {worldName(r.world_id)} · {STATUS_LABEL[r.status]}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </main>
  );
}

export function GateCard({
  gate,
  world,
  onAnswer,
}: {
  gate: Gate;
  world: string;
  onAnswer: Answer;
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
      const budget = Math.round(total * 100) / 100;
      await onAnswer(gate, () => setBudget(gate.run_id, budget), `You raised the budget to ${money(budget)}`);
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
    const said = body.send_back_to
      ? `You sent it back to ${body.send_back_to}`
      : gate.kind === "pick"
        ? `You picked "${body.choice}"`
        : gate.kind === "confirm"
          ? "You confirmed it"
          : "You approved it";
    setBusy(true);
    await onAnswer(gate, () => decide(gate.run_id, body), said);
    setBusy(false);
    setSendBack(false);
  }

  async function abort() {
    if (!window.confirm(`Abort ${gate.run_name}? Finished stages keep their artifacts.`)) return;
    setBusy(true);
    await onAnswer(gate, () => actOn(gate.run_id, "abort"), "You aborted the run");
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
                  <span className="faint">
                    {gate.send_backs_left} of {gate.max_send_backs} revisions left
                  </span>
                </>
              ) : (
                <span className="faint">
                  No revisions left at this gate. Approve, or abort the run from its timeline.
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
  onAnswer: Answer;
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
    }, approve ? "You approved the proposal" : "You rejected the proposal");
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
