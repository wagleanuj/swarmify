import { type Connectors, labelOf } from "../api/connectors";
import { current, holdsSlot, type Run, STATUS_LABEL } from "../api/runs";

/** How a pod looks: the type of its run's current stage, or the gate it waits at. */
export type Kind = "swarm" | "connector" | "workflow" | "gate" | "retro" | "paused";

const DAY = 24 * 60 * 60 * 1000;
const ENDED = new Set<Run["status"]>(["done", "failed", "aborted"]);
const oldest = (a: Run, b: Run) => a.created_at.localeCompare(b.created_at);

/** The office from the world's runs: a pod per live top-level run, the queue on the sofa, runs
 * finished in the last day on the shelf. A child run shows on its parent's pod. */
export function scene(runs: Run[], now = Date.now()) {
  const top = runs.filter((r) => !r.parent_run_id);
  return {
    live: top.filter(holdsSlot).sort(oldest),
    queued: top.filter((r) => r.status === "queued").sort(oldest),
    shelf: top
      .filter((r) => ENDED.has(r.status) && now - Date.parse(r.updated_at) < DAY)
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
  };
}

export function kindOf(run: Run): Kind {
  if (run.status === "waiting_gate") return "gate";
  if (run.status === "paused") return "paused";
  const type = current(run)?.type;
  return type === "connector" || type === "workflow" || type === "gate" || type === "retro" ? type : "swarm";
}

/** "stage 7 of 12, Render scenes": a 30-stage run reads the same as a 5-stage one. */
export function stageLine(run: Run): string {
  const at = current(run);
  const total = run.stages.length;
  return at ? `stage ${at.index + 1} of ${total}, ${at.name}` : `stage ${total} of ${total}`;
}

/** What a pod's whiteboard says under the stage: the kind's label in words. */
export function kindLine(run: Run, runs: Run[], connectors: Connectors): string {
  const at = current(run);
  switch (kindOf(run)) {
    case "gate":
      return "Waiting on you";
    case "paused":
      return "Paused";
    case "connector":
      return `Sending to ${labelOf(connectors, at?.calls ?? at?.name ?? "")}`;
    case "retro":
      return "Retro, learning from the run";
    case "workflow": {
      if (!at?.child) return "Starting a child run";
      const child = runs.find((r) => r.id === at.child?.id);
      return `Child run ${at.child.name}${child ? `, ${stageLine(child)}` : ""}`;
    }
    case "swarm":
      return "Working";
  }
}

/** A pod's accessible name: "Weekly explainer #14, stage 7 of 12, Render scenes", plus what
 * the pod shows when it is not a plain working swarm. */
export function podName(run: Run, runs: Run[], connectors: Connectors): string {
  const name = `${run.name}, ${stageLine(run)}`;
  return kindOf(run) === "swarm" ? name : `${name}, ${lower(kindLine(run, runs, connectors))}`;
}

const lower = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

/** A run off the floor, as the sofa, shelf and quest board name it: "Weekly #9, queued". */
export const restName = (run: Run) => `${run.name}, ${STATUS_LABEL[run.status]}`;
