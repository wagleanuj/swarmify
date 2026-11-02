import { useId, useState } from "react";

import { type Card, markCard, useGateAsk } from "../api/assistant";
import { api } from "../api/client";
import { reloadGates } from "../api/gates";
import type { Gate, Run } from "../api/runs";
import { ProposedCard } from "./RunAssistant";

/** Ask or change at a gate (spec Run Assistant): under the gate's own form, a turn with the run's
 * assistant. Its proposals land here as change cards; at a publish gate the fields it fills go
 * into the gate's form through `onFill`, for the person to confirm. An applied change reloads the
 * gates, so the preview shows what will really go out. */
export function GateAsk({ gate, onFill }: { gate: Gate; onFill: (fields: Record<string, string>) => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [dismissed, setDismissed] = useState<string[]>([]);
  const id = useId();

  // The run the cards apply to, read again after each turn and change for its revision.
  const load = () =>
    api
      .GET("/api/runs/{run_id}", { params: { path: { run_id: gate.run_id } } })
      .then(({ data }) => data && setRun(data))
      .catch(() => undefined);
  const { turns, text, setText, asking, error, send, filled } = useGateAsk(gate, onFill, load);

  const shown = (card: Card) => !dismissed.includes(card.id) && run?.stages.find((s) => s.id === card.proposal.change.stage);
  return (
    <section className="gate-ask" aria-label="Ask or change">
      {turns.length > 0 && (
        <ol className="conversation" aria-label="Conversation">
          {turns.map(({ asked, said }) => (
            <li key={said.id} className="said said-assistant">
              <p className="faint">{asked}</p>
              <p>{said.text}</p>
              {filled(said) && <p className="notice">{filled(said)}</p>}
              {run &&
                (said.proposals ?? []).map((card) => {
                  const stage = shown(card);
                  return (
                    stage && (
                      <ProposedCard
                        key={card.id}
                        run={run}
                        stage={stage}
                        card={card}
                        onClose={() => {
                          setDismissed((d) => [...d, card.id]);
                          void markCard(gate.run_id, card, "dismissed");
                        }}
                        onApplied={(change) => {
                          void markCard(gate.run_id, card, "applied", change.id);
                          reloadGates();
                          void load();
                        }}
                      />
                    )
                  );
                })}
            </li>
          ))}
        </ol>
      )}
      {error && <p role="alert">{error}</p>}
      <form className="ask" onSubmit={send}>
        <label htmlFor={id}>Ask or change before you answer</label>
        <textarea id={id} rows={2} value={text} onChange={(e) => setText(e.target.value)} />
        <button type="submit" disabled={!text.trim() || asking}>
          {asking ? "Reading the run..." : "Send"}
        </button>
      </form>
      <p className="faint meta">Asking costs about $0.02 a question, from the world's budget.</p>
    </section>
  );
}
