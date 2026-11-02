import { type FormEvent, useId, useState } from "react";

import { askRun, type Card, markCard, type Said } from "../api/assistant";
import { api } from "../api/client";
import { reloadGates } from "../api/gates";
import type { Gate, Run } from "../api/runs";
import { ChangeCard } from "./ChangeCard";

/** Ask or change at a gate (spec Run Assistant): under the gate's own form, a turn with the run's
 * assistant. Its proposals land here as change cards; at a publish gate the fields it fills go
 * into the gate's form through `onFill`, for the person to confirm. An applied change reloads the
 * gates, so the preview shows what will really go out. */
export function GateAsk({ gate, onFill }: { gate: Gate; onFill: (fields: Record<string, string>) => void }) {
  const [turns, setTurns] = useState<{ asked: string; said: Said }[]>([]);
  const [run, setRun] = useState<Run | null>(null);
  const [text, setText] = useState("");
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<string[]>([]);
  const id = useId();

  // The run the cards apply to, read again after each turn and change for its revision.
  const load = () =>
    api
      .GET("/api/runs/{run_id}", { params: { path: { run_id: gate.run_id } } })
      .then(({ data }) => data && setRun(data))
      .catch(() => undefined);

  async function send(e: FormEvent) {
    e.preventDefault();
    const asked = text.trim();
    if (!asked || asking) return;
    setError(null);
    setAsking(true);
    const { said, refused } = await askRun(gate.run_id, asked);
    if (said) {
      setText("");
      setTurns((t) => [...t, { asked, said }]);
      if (Object.keys(said.fields ?? {}).length > 0) onFill(said.fields ?? {});
      await load();
    } else setError(refused ?? "The assistant did not answer");
    setAsking(false);
  }

  const labelOf = (key: string) => gate.publish?.fields?.find((f) => f.key === key)?.label ?? key;
  const shown = (card: Card) => !dismissed.includes(card.id) && run?.stages.find((s) => s.id === card.proposal.change.stage);
  return (
    <section className="gate-ask" aria-label="Ask or change">
      {turns.length > 0 && (
        <ol className="conversation" aria-label="Conversation">
          {turns.map(({ asked, said }) => (
            <li key={said.id} className="said said-assistant">
              <p className="faint">{asked}</p>
              <p>{said.text}</p>
              {Object.keys(said.fields ?? {}).length > 0 && (
                <p className="notice">Filled {Object.keys(said.fields ?? {}).map(labelOf).join(", ")} in the form above. Check it, then answer.</p>
              )}
              {run &&
                (said.proposals ?? []).map((card) => {
                  const stage = shown(card);
                  return (
                    stage && (
                      <ChangeCard
                        key={card.id}
                        run={run}
                        stage={stage}
                        proposed={card.proposal}
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
