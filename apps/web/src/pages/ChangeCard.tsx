import { useEffect, useId, useState } from "react";
import YAML from "yaml";

import { lines } from "../api/assistant";
import { api } from "../api/client";
import type { ArtifactFile } from "../api/events";
import { refusal, type Run } from "../api/runs";
import type { components } from "../api/schema";
import { FileLink, fileUrl } from "./Artifacts";
import { RunRef } from "./Runs";
import { SaveToWorkflow } from "./SaveToWorkflow";

type Stage = Run["stages"][number];
type Proposal = components["schemas"]["ChangeProposal"];
export type AppliedChange = components["schemas"]["AppliedChange"];

/** Who applied a change, as the page says it: the operator is "you". */
export const who = (by: string) => (by === "operator" ? "you" : by);
export const hhmm = (at: string) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/** A file an Artifact Edit may rewrite: text a finished stage made, never a binary. */
export const EDITABLE = /\.(md|markdown|json|ya?ml|txt)$/i;

export type ChangeKind = "stage_edit" | "restructure";

/** A Run Change as the page names it, in the glossary's words. */
export const changeLabel = (c: { kind: string; stage_name: string }) =>
  c.kind === "restructure" ? `Restructure from ${c.stage_name}` : `Stage Edit of ${c.stage_name}`;

/** A stage in the workflow's shape: its name, type and config fields together. */
const shaped = (s: Stage) => ({ name: s.name, type: s.type, ...(s.config ?? {}) });

/** A Stage Edit or a Restructure as a change card (ADR 0027): the stage's config, or the stages
 * from it on, to edit, the diff, what the check would refuse, and Apply only once it passes.
 * A change that continues as a Fork says so. Nothing changes until Apply. With a `file` it is
 * an Artifact Edit of that file the stage made: its text, a line diff, and the finished stages
 * that already read the old version. `proposed` is a Stage Edit or an Artifact Edit made
 * already (by the Run Assistant): shown as it was, so it goes stale when the run moved. */
export function ChangeCard({
  run,
  stage,
  file,
  kind: edit = "stage_edit",
  onClose,
  proposed,
  onApplied,
}: {
  run: Run;
  stage: Stage;
  file?: ArtifactFile;
  kind?: ChangeKind;
  onClose: () => void;
  proposed?: Proposal;
  onApplied?: (change: AppliedChange) => void;
}) {
  // An Artifact Edit the Run Assistant proposed names its file by the payload's path.
  const named = file?.name ?? (proposed?.change.kind === "artifact_edit" ? String(proposed.change.payload?.path ?? "") : null);
  const restructure = named === null && edit === "restructure";
  const [start] = useState<string | null>(() =>
    named !== null
      ? proposed
        ? String(proposed.after.text ?? "")
        : null
      : YAML.stringify(
          restructure ? run.stages.slice(stage.index).filter((s) => s.type !== "retro").map(shaped) : (proposed?.after ?? stage.config ?? {}),
        ),
  );
  const [text, setText] = useState(start);
  const [editing, setEditing] = useState(!proposed);
  const [proposal, setProposal] = useState<Proposal | null>(proposed ?? null);
  const [problem, setProblem] = useState<string | null>(null);
  const [applied, setApplied] = useState<AppliedChange | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [asked, setAsked] = useState(0);
  const kind = named !== null ? "artifact_edit" : edit;
  const configId = useId();

  useEffect(() => {
    if (!file || proposed) return;
    let gone = false;
    globalThis
      .fetch(fileUrl(run.world_id, file.path))
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error())))
      .then((t) => gone || setText(t))
      .catch(() => gone || setProblem(`${file.name} could not be read`));
    return () => {
      gone = true;
    };
  }, [file, run.world_id, proposed]);

  useEffect(() => {
    if (proposed && asked === 0 && text === start) return; // as proposed, until edited or asked again
    setRefused(null);
    if (text === null) return;
    let payload: unknown;
    try {
      payload = named !== null ? { path: named, text } : YAML.parse(text);
    } catch (e) {
      setProposal(null);
      return setProblem((e instanceof Error && e.message.split("\n")[0]) || "the text is not YAML");
    }
    if (restructure ? !Array.isArray(payload) : !payload || typeof payload !== "object" || Array.isArray(payload)) {
      setProposal(null);
      return setProblem(restructure ? "the stages are a list" : "the stage's config is a set of fields");
    }
    if (restructure) payload = { stages: payload };
    let stale = false; // a slower answer for an earlier text must not land
    const timer = setTimeout(async () => {
      const { data, error, response } = await api.POST("/api/runs/{run_id}/changes/propose", {
        params: { path: { run_id: run.id } },
        body: { kind, stage: stage.id, payload: payload as Record<string, unknown> },
      });
      if (stale) return;
      setProposal(data ?? null);
      setProblem(refusal(error, response, "The change could not be checked"));
    }, 300);
    return () => {
      stale = true;
      clearTimeout(timer);
    };
  }, [text, run.id, stage.id, asked, named, kind, restructure, proposed, start]);

  async function apply() {
    if (!proposal) return;
    const { data, error, response } = await api.POST("/api/runs/{run_id}/changes", {
      params: { path: { run_id: run.id } },
      body: { kind, stage: stage.id, payload: proposal.after, revision: proposal.revision },
    });
    if (data) {
      onApplied?.(data);
      return setApplied(data);
    }
    setRefused(refusal(error, response, "The change was refused"));
  }

  // A Restructure's diff is by position: the stage that was there and the one that will be.
  const byPosition = (v: Record<string, unknown>) =>
    Object.fromEntries(((v.stages as Record<string, unknown>[] | undefined) ?? []).map((s, i) => [`${stage.index + i + 1}`, s]));
  const before = restructure ? byPosition(proposal?.before ?? {}) : (proposal?.before ?? {});
  const after = restructure ? byPosition(proposal?.after ?? {}) : (proposal?.after ?? {});
  const changed = named !== null
    ? lines(String(before.text ?? ""), String(after.text ?? ""))
    : [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
  const readBy = proposal?.read_by ?? [];
  const title = restructure ? `Change the stages from ${stage.name} on` : `Edit ${named ?? stage.name}`;
  const stale = !applied && !!proposal && (run.revision ?? 0) > proposal.revision;
  const problems = [...(problem ? [problem] : []), ...(proposal?.violations ?? [])];
  const ready = !!proposal && changed.length > 0 && problems.length === 0 && !stale;
  const ended = ["done", "failed", "aborted"].includes(run.status);
  return (
    <section className="change-card" aria-label={title} data-testid="change-card">
      <h3>{title}</h3>
      {proposal && changed.length > 0 && named !== null ? (
        <ul className="diff" aria-label="The change">
          {changed.map((l, i) => (
            <li key={i}>{l.startsWith("-") ? <del>{l.slice(1)}</del> : <ins>{l.slice(1)}</ins>}</li>
          ))}
        </ul>
      ) : proposal && changed.length > 0 ? (
        <ul className="diff" aria-label="The change">
          {changed.map((k) => (
            <li key={k}>
              <b>{k}</b> {k in before && <del>{show(before[k], restructure)}</del>} {k in after && <ins>{show(after[k], restructure)}</ins>}
            </li>
          ))}
        </ul>
      ) : (
        !applied && <p className="faint">No change yet. Edit the {named !== null ? "text" : "fields"} below.</p>
      )}
      {readBy.length > 0 && !applied && (
        <p className="warn">
          {listed(readBy)} already read the old version. Re-run {readBy.length > 1 ? "them" : "it"}?
        </p>
      )}
      {!applied &&
        problems.map((p) => (
          <p className="stage-error" key={p}>
            This would not pass: {p.replace(/\.$/, "")}.
          </p>
        ))}
      {proposal && !proposal.in_place && !applied && (
        <p className="warn">
          Continues as a new run from {proposal.fork_from}
          {ended ? "." : "; this one ends."}
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
            {named !== null && typeof applied.before.kept === "string" && (
              <>
                {" · "}
                <FileLink a={{ path: applied.before.kept, name: named }} worldId={run.world_id}>
                  Earlier version
                </FileLink>
              </>
            )}
            {applied.fork && (
              <>
                . <RunRef label="Continues in" run={applied.fork} testId="continues-in" />
              </>
            )}
          </p>
          <SaveToWorkflow run={run} change={applied} />
        </>
      ) : (
        <>
          {editing && (
            <>
              <label htmlFor={configId}>{named !== null ? `${named}'s text` : restructure ? `The stages from ${stage.name} on` : `${stage.name}'s config`}</label>
              <textarea id={configId} value={text ?? ""} disabled={text === null} spellCheck={false} rows={10} onChange={(e) => setText(e.target.value)} />
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

/** "Check", "Check and Tag", "Check, Tag and Upload". */
const listed = (names: string[]) => (names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0]);

/** A config value in one line: text as is, anything else as JSON; a stage by its name and type. */
function show(value: unknown, stage = false): string {
  if (stage) {
    const { name, type, ...config } = value as Record<string, unknown>;
    return `${name} (${type}) ${JSON.stringify(config)}`;
  }
  return typeof value === "string" ? value : JSON.stringify(value);
}
