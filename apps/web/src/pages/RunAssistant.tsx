import { Link } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";

import { askingCosts, cardTitle } from "../api/assistant";
import { api } from "../api/client";
import { refusal, type Run } from "../api/runs";
import type { components } from "../api/schema";
import { type AppliedChange, ChangeCard, hhmm, who } from "./ChangeCard";
import { SaveToWorkflow } from "./SaveToWorkflow";
import { StepChangeCard } from "./StepChangeCard";

type Conversation = components["schemas"]["Conversation"];
type Card = components["schemas"]["Card"];

/** The Run Assistant (spec Run Assistant): ask the run in plain words; its answers link the
 * stages they read and its proposed changes come as change cards, applied through the Run
 * Change seam. Nothing changes until you apply. `onSpent` hears what asking has cost. */
export function RunAssistant({ run, onSpent }: { run: Run; onSpent: (usd: number) => void }) {
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [text, setText] = useState("");
  const [asking, setAsking] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    const { data } = await api.GET("/api/runs/{run_id}/assistant", { params: { path: { run_id: run.id } } });
    if (!data) return;
    setConversation(data);
    onSpent(data.spent_usd);
  }
  useEffect(() => {
    load().catch(() => setError("Could not load the conversation. Is the API running?"));
  }, [run.id]); // eslint-disable-line react-hooks/exhaustive-deps

  async function send(e: FormEvent) {
    e.preventDefault();
    const message = text.trim();
    if (!message || asking) return;
    setError(null);
    setAsking(message);
    setText("");
    const { error: refused, response } = await api.POST("/api/runs/{run_id}/assistant", {
      params: { path: { run_id: run.id } },
      body: { message },
    });
    if (refused) {
      setText(message);
      setError(refusal(refused, response, "The assistant did not answer"));
    } else await load();
    setAsking(null);
  }

  async function mark(card: Card, state: "applied" | "dismissed", changeId?: string) {
    const { data } = await api.POST("/api/runs/{run_id}/assistant/proposals/{proposal_id}", {
      params: { path: { run_id: run.id, proposal_id: card.id } },
      body: { state, change_id: changeId ?? null },
    });
    // An applied card stays as it is, saying who applied it; the record is for a reload.
    if (!data || !conversation || state === "applied") return;
    const messages = conversation.messages.map((m) => ({ ...m, proposals: m.proposals?.map((p) => (p.id === data.id ? data : p)) }));
    setConversation({ ...conversation, messages });
  }

  return (
    <section className="panel assistant" aria-label="Ask this run" data-testid="assistant">
      <h2>Ask this run</h2>
      <p className="faint">It reads the run and proposes changes. Nothing changes until you apply.</p>
      <ol className="conversation" aria-label="Conversation">
        {conversation?.messages.map((m) => (
          <li key={m.id} className={`said said-${m.role}`}>
            <p>{m.text}</p>
            {(m.sources ?? []).length > 0 && (
              <p className="sources">
                {(m.sources ?? []).map((s, n) => (
                  <Link key={n} to="/worlds/$worldId/runs/$runId" params={{ worldId: run.world_id, runId: run.id }} search={{ stage: s.stage }}>
                    {s.label}
                  </Link>
                ))}
              </p>
            )}
            {(m.proposals ?? []).map((card) => (
              <Proposed key={card.id} run={run} card={card} onMark={(state, id) => mark(card, state, id)} />
            ))}
          </li>
        ))}
        {asking && (
          <li className="said said-operator">
            <p>{asking}</p>
            <p className="faint" role="status">
              Reading the run...
            </p>
          </li>
        )}
      </ol>
      {error && <p role="alert">{error}</p>}
      <form className="ask" onSubmit={send}>
        <label htmlFor="ask-run" className="visually-hidden">
          Ask this run
        </label>
        <textarea id="ask-run" rows={2} placeholder="Ask about this run, or say what to change" value={text} onChange={(e) => setText(e.target.value)} />
        <button type="submit" className="primary" disabled={!text.trim() || !!asking}>
          Send
        </button>
      </form>
      <p className="faint meta">{askingCosts(conversation)}</p>
    </section>
  );
}

/** One proposal in the conversation: the change card while open or stale, a line once applied,
 * nothing once dismissed with Not now. */
function Proposed({ run, card, onMark }: { run: Run; card: Card; onMark: (state: "applied" | "dismissed", changeId?: string) => void }) {
  const stage = run.stages.find((s) => s.id === card.proposal.change.stage);
  if (card.state === "dismissed" || !stage) return null;
  if (card.state === "applied") {
    const change = (run.changes ?? []).find((c) => c.id === card.change_id);
    return (
      <>
        <p className="notice ok">
          {cardTitle(card)}. {change ? `Applied by ${who(change.by)} at ${hhmm(change.at)}` : "Applied"}
        </p>
        {change && <SaveToWorkflow run={run} change={change} />}
      </>
    );
  }
  return <ProposedCard run={run} stage={stage} card={card} onClose={() => onMark("dismissed")} onApplied={(c) => onMark("applied", c.id)} />;
}

/** The change card for a proposal of any kind, as proposed: a Rerun or a Skip as a step change,
 * the others as a change card. Shared by the run page and the gate panel. */
export function ProposedCard({
  run,
  stage,
  card,
  onClose,
  onApplied,
}: {
  run: Run;
  stage: Run["stages"][number];
  card: Card;
  onClose: () => void;
  onApplied: (change: AppliedChange) => void;
}) {
  const { kind } = card.proposal.change;
  if (kind === "rerun" || kind === "skip")
    return <StepChangeCard run={run} stage={stage} kind={kind} proposed={card.proposal} onClose={onClose} onApplied={onApplied} />;
  const edit = kind === "restructure" ? "restructure" : "stage_edit";
  return <ChangeCard run={run} stage={stage} kind={edit} proposed={card.proposal} onClose={onClose} onApplied={onApplied} />;
}
