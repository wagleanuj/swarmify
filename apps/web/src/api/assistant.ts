/** Asking a run's Run Assistant from a gate, shared by the Needs you page and a skin's own gate
 * screen (Talk), so both ask, apply and dismiss alike. */
import { api } from "./client";
import { refusal } from "./runs";
import type { components } from "./schema";

export type Card = components["schemas"]["Card"];
export type Said = components["schemas"]["Message-Output"];
type Kind = components["schemas"]["ApplyChange"]["kind"];

/** One turn: the assistant's reply, or why it did not answer. */
export async function askRun(runId: string, message: string): Promise<{ said?: Said; refused?: string }> {
  const { data, error, response } = await api.POST("/api/runs/{run_id}/assistant", {
    params: { path: { run_id: runId } },
    body: { message },
  });
  return data ? { said: data } : { refused: refusal(error, response, "The assistant did not answer") ?? undefined };
}

/** Apply a proposal as proposed, through the Run Change seam, and record it on the card. Resolves
 * to the refusal (the run moved on, the check failed), or null once applied. */
export async function applyCard(runId: string, card: Card): Promise<string | null> {
  const { change, after, revision } = card.proposal;
  const { data, error, response } = await api.POST("/api/runs/{run_id}/changes", {
    params: { path: { run_id: runId } },
    body: { kind: change.kind as Kind, stage: change.stage, payload: after, revision },
  });
  if (!data) return refusal(error, response, "The change was refused");
  await markCard(runId, card, "applied", data.id);
  return null;
}

/** Record what became of a proposal: applied (naming the change) or dismissed with Not now. */
export async function markCard(runId: string, card: Card, state: "applied" | "dismissed", changeId?: string) {
  await api.POST("/api/runs/{run_id}/assistant/proposals/{proposal_id}", {
    params: { path: { run_id: runId, proposal_id: card.id } },
    body: { state, change_id: changeId ?? null },
  });
}

/** A change card's title by kind, in the spec's words: "Edit metadata.md", "Re-run from Script". */
export function cardTitle(card: Card): string {
  const { change, stage_name } = card.proposal;
  if (change.kind === "artifact_edit") return `Edit ${String(change.payload?.path ?? stage_name)}`;
  if (change.kind === "rerun") return `Re-run from ${stage_name}`;
  if (change.kind === "skip") return `Skip ${stage_name}`;
  if (change.kind === "restructure") return `Change the stages from ${stage_name} on`;
  return `Edit ${stage_name}`;
}

/** The lines that differ, `-` before and `+` after, around the lines both share at the top and
 * bottom: enough for the one place a person edits a file. */
export function lines(before: string, after: string): string[] {
  const a = before.split("\n");
  const b = after.split("\n");
  let top = 0;
  while (top < a.length && top < b.length && a[top] === b[top]) top++;
  let end = 0;
  while (end < a.length - top && end < b.length - top && a[a.length - 1 - end] === b[b.length - 1 - end]) end++;
  return [...a.slice(top, a.length - end).map((l) => `-${l}`), ...b.slice(top, b.length - end).map((l) => `+${l}`)];
}
