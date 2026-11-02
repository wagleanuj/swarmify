import { useEffect, useState } from "react";
import YAML from "yaml";

import { api } from "../api/client";
import { refusal, type Run } from "../api/runs";
import type { components } from "../api/schema";

type Stage = Run["stages"][number];
type Proposal = components["schemas"]["ChangeProposal"];
export type AppliedChange = components["schemas"]["AppliedChange"];

/** Who applied a change, as the page says it: the operator is "you". */
export const who = (by: string) => (by === "operator" ? "you" : by);
export const hhmm = (at: string) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/** A Stage Edit as a change card (ADR 0027): the stage's config to edit, the diff, what the
 * check would refuse, and Apply only once it passes. Nothing changes until Apply. */
export function ChangeCard({ run, stage, onClose }: { run: Run; stage: Stage; onClose: () => void }) {
  const [text, setText] = useState(() => YAML.stringify(stage.config ?? {}));
  const [editing, setEditing] = useState(true);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [applied, setApplied] = useState<AppliedChange | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [asked, setAsked] = useState(0);

  useEffect(() => {
    setRefused(null);
    let payload: unknown;
    try {
      payload = YAML.parse(text);
    } catch (e) {
      setProposal(null);
      return setProblem((e instanceof Error && e.message.split("\n")[0]) || "the text is not YAML");
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      setProposal(null);
      return setProblem("the stage's config is a set of fields");
    }
    let stale = false; // a slower answer for an earlier text must not land
    const timer = setTimeout(async () => {
      const { data, error, response } = await api.POST("/api/runs/{run_id}/changes/propose", {
        params: { path: { run_id: run.id } },
        body: { kind: "stage_edit", stage: stage.id, payload: payload as Record<string, unknown> },
      });
      if (stale) return;
      setProposal(data ?? null);
      setProblem(refusal(error, response, "The change could not be checked"));
    }, 300);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [text, run.id, stage.id, asked]);

  async function apply() {
    if (!proposal) return;
    const { data, error, response } = await api.POST("/api/runs/{run_id}/changes", {
      params: { path: { run_id: run.id } },
      body: { kind: "stage_edit", stage: stage.id, payload: proposal.after, revision: proposal.revision },
    });
    if (data) return setApplied(data);
    setRefused(refusal(error, response, "The change was refused"));
  }

  const before = proposal?.before ?? {};
  const after = proposal?.after ?? {};
  const changed = [...new Set([...Object.keys(before), ...Object.keys(after)])].filter(
    (k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]),
  );
  const stale = !applied && !!proposal && (run.revision ?? 0) > proposal.revision;
  const problems = [...(problem ? [problem] : []), ...(proposal?.violations ?? [])];
  const ready = !!proposal && changed.length > 0 && problems.length === 0 && !stale;
  return (
    <section className="change-card" aria-label={`Edit ${stage.name}`} data-testid="change-card">
      <h3>Edit {stage.name}</h3>
      {proposal && changed.length > 0 ? (
        <ul className="diff" aria-label="The change">
          {changed.map((k) => (
            <li key={k}>
              <b>{k}</b> {k in before && <del>{show(before[k])}</del>} {k in after && <ins>{show(after[k])}</ins>}
            </li>
          ))}
        </ul>
      ) : (
        !applied && <p className="faint">No change yet. Edit the fields below.</p>
      )}
      {!applied &&
        problems.map((p) => (
          <p className="stage-error" key={p}>
            This would not pass: {p.replace(/\.$/, "")}.
          </p>
        ))}
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
        <p className="notice ok" role="status">
          Applied by {who(applied.by)} at {hhmm(applied.at)}
        </p>
      ) : (
        <>
          {editing && (
            <>
              <label htmlFor="stage-config">{stage.name}'s config</label>
              <textarea id="stage-config" value={text} spellCheck={false} rows={10} onChange={(e) => setText(e.target.value)} />
            </>
          )}
          <div className="answer-bar">
            <button type="button" className="primary" disabled={!ready} onClick={apply}>
              Apply
            </button>
            <button type="button" aria-pressed={editing} onClick={() => setEditing(!editing)}>
              Edit
            </button>
            <button type="button" onClick={onClose}>
              Not now
            </button>
          </div>
        </>
      )}
    </section>
  );
}

/** A config value in one line: text as is, anything else as JSON. */
function show(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}
