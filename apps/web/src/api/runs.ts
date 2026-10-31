import { api } from "./client";
import type { Connectors } from "./connectors";
import { type Call, type Run, useWorldEvents } from "./events";
import type { components } from "./schema";

export type { Call, Run };
export type Gate = components["schemas"]["Gate"];
export type Decision = components["schemas"]["Decision"];
export type Decide = components["schemas"]["Decide"];
export type RunLink = components["schemas"]["RunLink"];

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

type Stage = Run["stages"][number];
const FINISHED = ["done", "skipped"];

/** The stage a run is at: the one working or waiting, else the first not finished. */
export function current(run: Run): Stage | undefined {
  return (
    run.stages.find((s) => s.status === "running" || s.status === "waiting") ??
    run.stages.find((s) => !FINISHED.includes(s.status))
  );
}

/** One sentence that answers "is it working, and do I need to do anything?". `ask` is what the
 * open gate asks, when the run waits at one; `connectors` say which steps post. */
export function statusLine(run: Run, ask?: string | null, connectors: Connectors = {}, now = Date.now()): string {
  const line = sentence(run, ask, connectors, now);
  return line.charAt(0).toUpperCase() + line.slice(1);
}

function sentence(run: Run, ask: string | null | undefined, connectors: Connectors, now: number): string {
  const at = current(run);
  const total = run.stages.length;
  const seconds = duration(run, now);
  const kept = " Finished stages keep their artifacts.";
  // A run that has a step that posts and did not reach it says nothing went out.
  const posted = run.stages.some((s) => connectors[s.calls ?? ""]?.kind === "publish" && s.status !== "done");
  const nothing = posted ? " Nothing was posted." : "";
  switch (run.status) {
    case "queued":
      return "Queued. It starts as soon as one of this world's two run slots is free.";
    case "running": {
      if (!at) return "Running.";
      const step = ` · step ${at.index + 1} of ${total}${seconds === null ? "" : ` · ${took(seconds)} so far`}`;
      if (at.child) return `${at.name} waits on its child run ${at.child.name}${step}`;
      return `${at.name} is working${step}`;
    }
    case "waiting_gate":
      return `Waiting for you: ${ask ?? at?.ask ?? (at ? `${at.name} needs an answer.` : "a gate needs an answer.")}`;
    case "paused":
      return at ? `Paused at ${at.name}.` : "Paused.";
    case "done":
      return seconds === null ? "Done." : `Done in ${took(seconds)}.`;
    case "failed": {
      const failed = run.stages.find((s) => s.status === "failed");
      const why = (failed?.error?.split("\n")[0] ?? "no error recorded").replace(/([^.!?])$/, "$1.");
      return `${failed ? `Stopped at ${failed.name}: ${why}` : "Stopped."}${kept}${nothing}`;
    }
    case "aborted":
      return `${at ? `Aborted at ${at.name}.` : "Aborted."}${kept}${nothing}`;
  }
}

/** A step's duration: "14s", "2m 14s", "1h 5m". */
export function clock(seconds: number): string {
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

/** What a person decided, as one line of plain copy. */
export function decisionLine(d: Decision): string {
  const note = d.note ? ` Your note: ${d.note}` : "";
  const what = d.choice ? ` "${d.choice}"` : "";
  if (d.send_back_to) return `You sent it back to ${d.send_back_to}.${note}`;
  if (d.kind === "pick") return `You picked${what || " nothing"}.${note}`;
  if (d.kind === "confirm") return `You confirmed.${note}`;
  return `You approved${what}.${note}`;
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

/** Set the run's budget to `total` dollars (a total, so a double click adds nothing twice). */
export async function setBudget(runId: string, total: number): Promise<string | null> {
  const { error, response } = await api.POST("/api/runs/{run_id}/budget", {
    params: { path: { run_id: runId } },
    body: { budget_usd: total },
  });
  return refusal(error, response, "The budget was refused");
}

/** Dollars to the cent, for budgets and spend. */
export const money = (n: number) => `$${n.toFixed(2)}`;

/** Seconds from the first stage starting to the last ending (or now, while it runs). */
export function duration(run: Run, now = Date.now()): number | null {
  const starts = run.stages.flatMap((s) => (s.started_at ? [Date.parse(s.started_at)] : []));
  if (starts.length === 0) return null;
  const ended = ["done", "failed", "aborted"].includes(run.status);
  const ends = run.stages.flatMap((s) => (s.ended_at ? [Date.parse(s.ended_at)] : []));
  const end = ended && ends.length > 0 ? Math.max(...ends) : now;
  return Math.max(0, (end - Math.min(...starts)) / 1000);
}

/** "40 s", "54 min", "2 h 5 min". */
export function took(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

export async function decide(runId: string, body: Decide): Promise<string | null> {
  const { error, response } = await api.POST("/api/runs/{run_id}/decision", {
    params: { path: { run_id: runId } },
    body,
  });
  return refusal(error, response, "The decision was refused");
}

/** The API's refusal detail, or `fallback (status).`; null when there was no error. */
export function refusal(error: unknown, response: Response, fallback: string): string | null {
  if (!error) return null;
  const detail = (error as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : `${fallback} (${response.status}).`;
}
