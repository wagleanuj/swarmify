import { useEffect, useState } from "react";

import { api } from "../api/client";
import { money, refusal, type Run } from "../api/runs";
import type { components } from "../api/schema";
import { type AppliedChange, hhmm, who } from "./ChangeCard";
import { SaveToWorkflow } from "./SaveToWorkflow";

type Stage = Run["stages"][number];
type Proposal = components["schemas"]["ChangeProposal"];
export type StepKind = "rerun" | "skip";

/** What an applied change did, as the Timeline and the change list say it. */
export const changed = (kind: string) => ({ rerun: "Re-run", skip: "Skipped" })[kind] ?? "Edited";
export const changedStage = (c: AppliedChange) =>
  c.kind === "rerun"
    ? `Re-run from ${c.stage_name}`
    : c.kind === "artifact_edit"
      ? `Edited ${String(c.payload.path)} of ${c.stage_name}`
      : `${changed(c.kind)} ${c.stage_name}`;

const names = new Intl.ListFormat("en-GB", { type: "conjunction" });

/** A Rerun or a Skip as a change card (ADR 0027): the steps it resets or passes over, what a
 * Rerun costs again against the budget left, what the check would refuse, and Apply only once it
 * passes. Nothing changes until Apply. */
export function StepChangeCard({ run, stage, kind, onClose }: { run: Run; stage: Stage; kind: StepKind; onClose: () => void }) {
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(kind === "rerun");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [applied, setApplied] = useState<AppliedChange | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [asked, setAsked] = useState(0);

  useEffect(() => {
    let stale = false; // a slower answer for an earlier ask must not land
    (async () => {
      const { data, error, response } = await api.POST("/api/runs/{run_id}/changes/propose", {
        params: { path: { run_id: run.id } },
        body: { kind, stage: stage.id },
      });
      if (stale) return;
      setRefused(null);
      setProposal(data ?? null);
      setProblem(refusal(error, response, "The change could not be checked"));
    })();
    return () => {
      stale = true;
    };
  }, [run.id, stage.id, kind, asked]);

  async function apply() {
    if (!proposal) return;
    const { data, error, response } = await api.POST("/api/runs/{run_id}/changes", {
      params: { path: { run_id: run.id } },
      body: { kind, stage: stage.id, payload: kind === "rerun" ? { note: note.trim() } : {}, revision: proposal.revision },
    });
    if (data) return setApplied(data);
    setRefused(refusal(error, response, "The change was refused"));
  }

  const title = kind === "rerun" ? `Re-run from ${stage.name}` : `Skip ${stage.name}`;
  const before = proposal?.before ?? {};
  const after = proposal?.after ?? {};
  const stale = !applied && !!proposal && (run.revision ?? 0) > proposal.revision;
  const problems = [...(problem ? [problem] : []), ...(proposal?.violations ?? [])];
  const ready = !!proposal && problems.length === 0 && !stale;
  const left = proposal?.budget_left_usd;
  const cost = proposal?.cost_usd;
  return (
    <section className="change-card" aria-label={title} data-testid="change-card">
      <h3>{title}</h3>
      {proposal && (
        <ul className="diff" aria-label="The change">
          {Object.keys(before).map((k) => (
            <li key={k}>
              <b>{k}</b> <del>{String(before[k])}</del> <ins>{String(after[k])}</ins>
            </li>
          ))}
        </ul>
      )}
      {!applied &&
        problems.map((p) => (
          <p className="stage-error" key={p}>
            This would not pass: {p.replace(/\.$/, "")}.
          </p>
        ))}
      {proposal && kind === "rerun" && cost != null && (
        <p className="faint">
          Resets {names.format(Object.keys(before))}, about {money(cost)} more{left != null && ` of the ${money(left)} left`}
        </p>
      )}
      {refused && !applied && (
        <p className="stage-error" role="alert">
          {refused}
        </p>
      )}
      {stale && (
        <p className="warn" role="status">
          The run moved on since this was proposed.{" "}
          <button type="button" onClick={() => setAsked((n) => n + 1)}>
            Ask again
          </button>
        </p>
      )}
      {applied ? (
        <>
          <p className="notice ok" role="status">
            Applied by {who(applied.by)} at {hhmm(applied.at)}
          </p>
          <SaveToWorkflow run={run} change={applied} />
        </>
      ) : (
        <>
          {editing && kind === "rerun" && (
            <>
              <label htmlFor="rerun-note">Note for {stage.name}</label>
              <textarea id="rerun-note" value={note} rows={3} placeholder="What to do differently" onChange={(e) => setNote(e.target.value)} />
            </>
          )}
          <div className="answer-bar">
            <button type="button" className="primary" disabled={!ready} onClick={apply}>
              Apply
            </button>
            {kind === "rerun" && (
              <button type="button" aria-pressed={editing} onClick={() => setEditing(!editing)}>
                Edit
              </button>
            )}
            <button type="button" onClick={onClose}>
              Not now
            </button>
          </div>
        </>
      )}
    </section>
  );
}
