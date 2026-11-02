import { useEffect, useState } from "react";
import YAML from "yaml";

import { api } from "../api/client";
import type { ArtifactFile } from "../api/events";
import { refusal, type Run } from "../api/runs";
import type { components } from "../api/schema";
import { FileLink, fileUrl } from "./Artifacts";

type Stage = Run["stages"][number];
type Proposal = components["schemas"]["ChangeProposal"];
export type AppliedChange = components["schemas"]["AppliedChange"];

/** Who applied a change, as the page says it: the operator is "you". */
export const who = (by: string) => (by === "operator" ? "you" : by);
export const hhmm = (at: string) => new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/** A file an Artifact Edit may rewrite: text a finished stage made, never a binary. */
export const EDITABLE = /\.(md|markdown|json|ya?ml|txt)$/i;

/** A Stage Edit as a change card (ADR 0027): the stage's config to edit, the diff, what the
 * check would refuse, and Apply only once it passes. Nothing changes until Apply. With a `file`
 * it is an Artifact Edit of that file the stage made: its text, a line diff, and the finished
 * stages that already read the old version. */
export function ChangeCard({ run, stage, file, onClose }: { run: Run; stage: Stage; file?: ArtifactFile; onClose: () => void }) {
  const [text, setText] = useState<string | null>(() => (file ? null : YAML.stringify(stage.config ?? {})));
  const [editing, setEditing] = useState(true);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [applied, setApplied] = useState<AppliedChange | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const [asked, setAsked] = useState(0);
  const kind = file ? "artifact_edit" : "stage_edit";

  useEffect(() => {
    if (!file) return;
    let gone = false;
    globalThis
      .fetch(fileUrl(run.world_id, file.path))
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error())))
      .then((t) => gone || setText(t))
      .catch(() => gone || setProblem(`${file.name} could not be read`));
    return () => {
      gone = true;
    };
  }, [file, run.world_id]);

  useEffect(() => {
    setRefused(null);
    if (text === null) return;
    let payload: unknown;
    try {
      payload = file ? { path: file.name, text } : YAML.parse(text);
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
  }, [text, run.id, stage.id, asked, file, kind]);

  async function apply() {
    if (!proposal) return;
    const { data, error, response } = await api.POST("/api/runs/{run_id}/changes", {
      params: { path: { run_id: run.id } },
      body: { kind, stage: stage.id, payload: proposal.after, revision: proposal.revision },
    });
    if (data) return setApplied(data);
    setRefused(refusal(error, response, "The change was refused"));
  }

  const before = proposal?.before ?? {};
  const after = proposal?.after ?? {};
  const changed = file
    ? lines(String(before.text ?? ""), String(after.text ?? ""))
    : [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((k) => JSON.stringify(before[k]) !== JSON.stringify(after[k]));
  const readBy = proposal?.read_by ?? [];
  const title = `Edit ${file?.name ?? stage.name}`;
  const stale = !applied && !!proposal && (run.revision ?? 0) > proposal.revision;
  const problems = [...(problem ? [problem] : []), ...(proposal?.violations ?? [])];
  const ready = !!proposal && changed.length > 0 && problems.length === 0 && !stale;
  return (
    <section className="change-card" aria-label={title} data-testid="change-card">
      <h3>{title}</h3>
      {proposal && changed.length > 0 && file ? (
        <ul className="diff" aria-label="The change">
          {changed.map((l, i) => (
            <li key={i}>{l.startsWith("-") ? <del>{l.slice(1)}</del> : <ins>{l.slice(1)}</ins>}</li>
          ))}
        </ul>
      ) : proposal && changed.length > 0 ? (
        <ul className="diff" aria-label="The change">
          {changed.map((k) => (
            <li key={k}>
              <b>{k}</b> {k in before && <del>{show(before[k])}</del>} {k in after && <ins>{show(after[k])}</ins>}
            </li>
          ))}
        </ul>
      ) : (
        !applied && <p className="faint">No change yet. Edit the {file ? "text" : "fields"} below.</p>
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
          {file && typeof applied.before.kept === "string" && (
            <>
              {" · "}
              <FileLink a={{ path: applied.before.kept, name: file.name }} worldId={run.world_id}>
                Earlier version
              </FileLink>
            </>
          )}
        </p>
      ) : (
        <>
          {editing && (
            <>
              <label htmlFor="stage-config">{file ? `${file.name}'s text` : `${stage.name}'s config`}</label>
              <textarea id="stage-config" value={text ?? ""} disabled={text === null} spellCheck={false} rows={10} onChange={(e) => setText(e.target.value)} />
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

/** The lines that differ, `-` before and `+` after, around the lines both share at the top and
 * bottom: enough for the one place a person edits a file. */
function lines(before: string, after: string): string[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let top = 0;
  while (top < a.length && top < b.length && a[top] === b[top]) top++;
  let end = 0;
  while (end < a.length - top && end < b.length - top && a[a.length - 1 - end] === b[b.length - 1 - end]) end++;
  return [...a.slice(top, a.length - end).map((l) => `-${l}`), ...b.slice(top, b.length - end).map((l) => `+${l}`)];
}

/** "Check", "Check and Tag", "Check, Tag and Upload". */
const listed = (names: string[]) => (names.length > 1 ? `${names.slice(0, -1).join(", ")} and ${names.at(-1)}` : names[0]);

/** A config value in one line: text as is, anything else as JSON. */
function show(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}
