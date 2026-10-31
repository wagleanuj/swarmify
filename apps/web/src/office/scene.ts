import { type Connectors, labelOf } from "../api/connectors";
import type { Agent, ArtifactCreated, LogEntry } from "../api/events";
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

/** The poses a sprite pack can draw; any it lacks falls back to idle. */
export type Pose = "idle" | "think" | "work" | "walk" | "talk" | "cheer" | "stuck" | "hold" | "back";

/** A pose in words, for the character's accessible name; idle says nothing. */
export const POSE_WORDS: Record<Pose, string> = {
  idle: "",
  think: "thinking",
  work: "working",
  walk: "walking",
  talk: "asking",
  cheer: "shipped",
  stuck: "stuck",
  hold: "carrying",
  back: "at the whiteboard",
};

export type Seat = { agent: Agent; pose: Pose };

/** Who is in a live run's pod and how they stand, from the data alone. On a swarm stage the
 * run's agents on it sit at the desks working, the planner thinking; on the retro stage they
 * work at the whiteboard; a failed agent, or any in a paused run, is stuck. At a gate the desks
 * empty and one agent asks: the judge of the stage before when it was judged, else the run's
 * planner, else the cast's planner (named `planner`) stands in. */
export function crew(run: Run, agents: Agent[], planner?: string): Seat[] {
  const at = current(run);
  if (!at) return [];
  const mine = agents.filter((a) => a.run_id === run.id);
  const kind = kindOf(run);
  if (kind === "gate") {
    const before = run.stages[at.index - 1]?.id;
    const asker =
      mine.filter((a) => a.role === "judge" && a.stage_id === before).at(-1) ??
      mine.filter((a) => a.role === "planner").at(-1) ??
      (planner ? standIn(run, at.id, planner) : undefined);
    return asker ? [{ agent: asker, pose: "talk" }] : [];
  }
  return mine
    .filter((a) => a.stage_id === at.id && a.status !== "done")
    .map((agent) => ({
      agent,
      pose: agent.status === "failed" || kind === "paused" ? "stuck" : kind === "retro" ? "back" : agent.role === "planner" ? "think" : "work",
    }));
}

/** The cast's planner standing in at a gate of a run with no agent of its own. */
const standIn = (run: Run, stageId: string, name: string): Agent => ({
  id: `${run.id}/stand-in`,
  world_id: run.world_id,
  run_id: run.id,
  stage_id: stageId,
  name,
  role: "planner",
  status: "working",
  session_id: null,
  cost_usd: 0,
  created_at: run.updated_at,
  updated_at: run.updated_at,
});

/** How a cast member off the floor stands: shipped when their latest run is done, stuck when it
 * (or they) failed, else idle. */
export function benchPose(name: string, agents: Agent[], runs: Run[]): Pose {
  const last = agents.filter((a) => a.name === name).sort((a, b) => a.updated_at.localeCompare(b.updated_at)).at(-1);
  if (!last) return "idle";
  const status = runs.find((r) => r.id === last.run_id)?.status;
  if (last.status === "failed" || status === "failed" || status === "aborted") return "stuck";
  return status === "done" ? "cheer" : "idle";
}

/** The top-level run a stage belongs to: a child run's stages show on its parent's pod. */
export function podOf(stageId: string, runs: Run[]): Run | undefined {
  const run = runs.find((r) => r.stages.some((s) => s.id === stageId));
  return run?.parent_run_id ? runs.find((r) => r.id === run.parent_run_id) : run;
}

const stageName = (stageId: string, runs: Run[]) => runs.flatMap((r) => r.stages).find((s) => s.id === stageId)?.name ?? "a stage";

/** A log line as a speech bubble: its first line, cut short; nothing for session bookkeeping. */
export function bubble(entry: LogEntry): string | null {
  const line = entry.detail.split("\n")[0]?.trim() ?? "";
  if (entry.kind === "session" || !line) return null;
  return line.length > 48 ? `${line.slice(0, 47)}\u2026` : line;
}

/** What the radio says about an agent whose status changed: "started Render scenes". */
export function agentNews(agent: Agent, was: Agent | undefined, runs: Run[]): string | null {
  if (was?.status === agent.status && was.stage_id === agent.stage_id) return null;
  const stage = stageName(agent.stage_id, runs);
  if (agent.status === "working") return `started ${stage}`;
  return agent.status === "done" ? `finished ${stage}` : `got stuck on ${stage}`;
}

export const artifactNews = (made: ArtifactCreated) => `made ${fileName(made.path)}`;

export const fileName = (path: string) => path.split("/").at(-1) ?? path;
