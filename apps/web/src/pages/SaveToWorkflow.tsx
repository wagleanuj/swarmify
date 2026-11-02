import { useId, useRef, useState } from "react";
import YAML from "yaml";

import { api } from "../api/client";
import { refusal, type Run } from "../api/runs";
import type { components } from "../api/schema";
import { type AppliedChange, lines } from "./ChangeCard";

type Draft = components["schemas"]["WorkflowDraft"];

/** "Also save to the workflow" under an applied Run Change, so the next run has the fix too:
 * the workflow's next version with the change in it, as the check sees it, saved only when the
 * operator says so, and twice when the workflow changed those stages since this run's version.
 * An Artifact Edit is saved as the Run Assistant's Stage Edit to the stage that made the file,
 * which the operator may edit first; it is drafted once and kept through "Not now".
 * A Rerun, or a run with no workflow, has nothing to save. */
export function SaveToWorkflow({ run, change }: { run: Run; change: AppliedChange }) {
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [text, setText] = useState("");
  const [bad, setBad] = useState<string | null>(null);
  const asked = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const configId = useId();
  if (!run.workflow_id || change.kind === "rerun") return null;

  function post(save: boolean, confirmed: boolean, cfg: Record<string, unknown> | null) {
    return api.POST("/api/runs/{run_id}/changes/{change_id}/save-to-workflow", {
      params: { path: { run_id: run.id, change_id: change.id } },
      body: { save, confirmed, version: save ? draft?.version : undefined, config: cfg ?? undefined },
    });
  }

  async function ask(save: boolean, confirmed = false, cfg = config) {
    const n = ++asked.current; // a slower answer for an earlier text must not land
    setBusy(true);
    const { data, error, response } = await post(save, confirmed, cfg);
    // A refused save shows the draft as it now stands, its conflicts and its next version.
    const fresh = save && !data ? (await post(false, false, cfg)).data : data;
    if (n !== asked.current) return;
    setBusy(false);
    if (fresh) {
      setDraft(fresh);
      setConfirming(false);
      if (!cfg && fresh.config) {
        setConfig(fresh.config);
        setText(YAML.stringify(fresh.config));
      }
    }
    setProblem(refusal(error, response, save ? "The workflow was not saved" : "The workflow change could not be drafted"));
  }

  function edit(t: string) {
    setText(t);
    clearTimeout(timer.current);
    let cfg: unknown;
    try {
      cfg = YAML.parse(t);
    } catch (e) {
      return setBad((e instanceof Error && e.message.split("\n")[0]) || "the text is not YAML");
    }
    if (!cfg || typeof cfg !== "object" || Array.isArray(cfg)) return setBad("the Stage Edit is a set of fields");
    setBad(null);
    timer.current = setTimeout(() => {
      setConfig(cfg as Record<string, unknown>);
      ask(false, false, cfg as Record<string, unknown>);
    }, 300);
  }

  if (draft?.saved) {
    return (
      <p className="notice ok" role="status">
        Saved as v{draft.saved.number} of the workflow.
      </p>
    );
  }
  if (!draft) {
    return (
      <div className="answer-bar">
        <button type="button" disabled={busy} onClick={() => ask(false)}>
          {busy ? "Drafting the workflow change" : "Also save to the workflow"}
        </button>
        {problem && (
          <p className="stage-error" role="alert">
            {problem}
          </p>
        )}
      </div>
    );
  }
  const next = draft.version + 1;
  const diff = lines(draft.before, draft.yaml);
  return (
    <section className="save-draft" aria-label="Save to the workflow">
      {draft.reply && <p>{draft.reply}</p>}
      <p className="faint">
        Saves the change to {draft.stage} as v{next} of the workflow. This run keeps v{draft.run_version}.
      </p>
      <ul className="diff" aria-label="The workflow change">
        {diff.map((l, i) => (
          <li key={i}>{l.startsWith("-") ? <del>{l.slice(1)}</del> : <ins>{l.slice(1)}</ins>}</li>
        ))}
      </ul>
      {config && (
        <>
          <label htmlFor={configId}>The Stage Edit to {draft.stage}</label>
          <textarea id={configId} value={text} spellCheck={false} rows={6} onChange={(e) => edit(e.target.value)} />
          {bad && <p className="stage-error">{bad}</p>}
        </>
      )}
      {draft.conflicts.map((c) => (
        <p className="warn" key={c}>
          {c}
        </p>
      ))}
      {draft.violations.map((v) => (
        <p className="stage-error" key={v.message}>
          This would not pass: {v.message.replace(/\.$/, "")}.
        </p>
      ))}
      {problem && (
        <p className="stage-error" role="alert">
          {problem}
        </p>
      )}
      {confirming && <p className="warn">The workflow changed since this run's version. Saving keeps those changes and adds this one.</p>}
      <div className="answer-bar">
        <button
          type="button"
          className="primary"
          disabled={busy || !!bad || draft.violations.length > 0}
          onClick={() => (draft.conflicts.length > 0 && !confirming ? setConfirming(true) : ask(true, confirming))}
        >
          {confirming ? "Save anyway" : `Save as v${next}`}
        </button>
        <button type="button" onClick={() => (setDraft(null), setConfirming(false))}>
          Not now
        </button>
      </div>
    </section>
  );
}
