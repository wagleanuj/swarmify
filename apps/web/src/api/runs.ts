import { api } from "./client";
import { type Call, type Run, useWorldEvents } from "./events";
import type { components } from "./schema";

export type { Call, Run };
export type Gate = components["schemas"]["Gate"];
export type Decision = components["schemas"]["Decision"];
export type Decide = components["schemas"]["Decide"];

/** Call `onRun` with every run snapshot the world's event stream carries (ADR 0002), and
 * `onCall` with every connector call snapshot, progress included. */
export function useRunUpdates(worldId: string, onRun: (run: Run) => void, onCall?: (call: Call) => void) {
  useWorldEvents(worldId, { "run.updated": onRun, "connector_call.updated": onCall });
}

export const STATUS_LABEL: Record<Run["status"], string> = {
  queued: "queued",
  running: "running",
  waiting_gate: "needs you",
  paused: "paused",
  done: "done",
  failed: "failed",
  aborted: "aborted",
};

export function progress(run: Run): string {
  const done = run.stages.filter((s) => s.status === "done").length;
  return `${done} of ${run.stages.length} stages`;
}

/** One confident sentence about where the run is. */
export function statusLine(run: Run): string {
  const at = run.stages.find((s) => s.status === "running" || s.status === "waiting");
  const failed = run.stages.find((s) => s.status === "failed");
  const total = run.stages.length;
  switch (run.status) {
    case "queued":
      return "Queued. It starts as soon as one of this world's two run slots is free.";
    case "running":
      return at ? `Running stage ${at.index + 1} of ${total}: ${at.name}.` : "Running.";
    case "waiting_gate":
      return at ? `Waiting for you at stage ${at.index + 1}: ${at.name}.` : "Waiting for you.";
    case "paused":
      return "Paused. Nothing moves until it is resumed.";
    case "done":
      return `Done. All ${total} stages finished.`;
    case "failed":
      return failed
        ? `Failed at stage ${failed.index + 1}, ${failed.name}: ${failed.error ?? "no error recorded"}.`
        : "Failed.";
    case "aborted":
      return "Aborted. Finished stages keep their artifacts.";
  }
}

/** What a person decided, as one line of plain copy. */
export function decisionLine(d: Decision): string {
  const note = d.note ? ` Note: ${d.note}` : "";
  if (d.send_back_to) return `You sent it back to ${d.send_back_to}.${note}`;
  if (d.kind === "pick") return `You picked ${d.choice ?? "nothing"}.${note}`;
  if (d.kind === "confirm") return `You confirmed.${note}`;
  return `You approved.${note}`;
}

/** Act on a run; resolves to the refused reason, or null when it went through. */
export async function actOn(
  runId: string,
  action: "pause" | "resume" | "abort",
): Promise<string | null> {
  const { error, response } = await api.POST(`/api/runs/{run_id}/${action}`, {
    params: { path: { run_id: runId } },
  });
  return refusal(error, response, `Could not ${action} the run`);
}

export async function decide(runId: string, body: Decide): Promise<string | null> {
  const { error, response } = await api.POST("/api/runs/{run_id}/decision", {
    params: { path: { run_id: runId } },
    body,
  });
  return refusal(error, response, "The decision was refused");
}

/** The API's refusal detail, or `fallback (status).`; null when there was no error. */
function refusal(error: unknown, response: Response, fallback: string): string | null {
  if (!error) return null;
  const detail = (error as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : `${fallback} (${response.status}).`;
}
