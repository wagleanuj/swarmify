/** Answering a gate: its choices and the decision each one posts, shared by every view that
 * answers gates (the plain Gates and run pages, and a skin's own screens), so they behave alike. */
import { api } from "./client";
import type { ArtifactFile } from "./events";
import { actOn, decide, type Decide, type Gate, money, refusal, setBudget } from "./runs";
import type { components } from "./schema";

export type Proposal = components["schemas"]["Proposal"];
type PublishField = components["schemas"]["PublishField"];

/** The kind of decision, as the queue names it. */
export const GATE_KIND: Record<Gate["kind"], string> = {
  pick: "Pick an idea",
  approve: "Review and approve",
  confirm: "Confirm before it goes out",
  budget: "Budget ran out",
  skill: "Review a skill change",
};

/** What the run wrote about itself, not something to choose or review. */
export const RECORD = new Set(["verdict", "decision", "note"]);

const byRank = (a: ArtifactFile, b: ArtifactFile) =>
  Number(a.fields.rank ?? 1e9) - Number(b.fields.rank ?? 1e9);

/** The cards a pick gate chooses from: the typed artifacts the judge projected from the earlier
 * stages (an idea per card), his rank first. A plain file is not a choice, so a stage with no
 * declared output leaves the gate its free-text pick. */
export function choosable(artifacts: ArtifactFile[]): ArtifactFile[] {
  return artifacts
    .filter((a) => !RECORD.has(a.type) && a.type !== "file" && !a.fields.deleted)
    .sort(byRank);
}

/** What an approve gate reviews: the artifacts of the latest earlier stage that made any. */
export function underReview(artifacts: ArtifactFile[]): ArtifactFile[] {
  const work = artifacts.filter((a) => !RECORD.has(a.type));
  const last = work.at(-1)?.stage;
  return work.filter((a) => a.stage === last);
}

/** What the earlier stages made, for a pick's cards or an approve's review. */
export async function gateArtifacts(gate: Gate): Promise<ArtifactFile[]> {
  const { data } = await api.GET("/api/runs/{run_id}/artifacts", {
    params: { path: { run_id: gate.run_id }, query: { before: gate.stage_index } },
  });
  return data ?? [];
}

/** The retro's proposal a skill gate decides, or null when it cannot be loaded. */
export async function gateProposal(gate: Gate): Promise<Proposal | null> {
  const { data } = await api.GET("/api/runs/{run_id}/proposal", { params: { path: { run_id: gate.run_id } } });
  return data ?? null;
}

/** The earlier steps an approve gate can send back to, nearest first; none once revisions run out. */
export const sendBackTargets = (gate: Gate) => (gate.send_backs_left > 0 ? [...gate.earlier_stages].reverse() : []);

/** A post that cannot go out would fail the run, so a gate in front of one cannot pass. */
export const blocked = (gate: Gate) => (gate.publish?.problems ?? []).length > 0;

/** A field the gate lets the person change: the connector marks it editable and it is text. */
export const editable = (f: PublishField) => f.editable && (typeof f.value === "string" || f.value == null);

/** The top-up a budget gate suggests: its budget again. */
export const topUpSuggestion = (gate: Gate) => Number((gate.budget_usd ?? 1).toFixed(2));

export type Choice =
  | { kind: "pick"; choice: string; artifact?: string }
  | { kind: "approve" }
  | { kind: "send_back"; to: string }
  | { kind: "confirm" }
  | { kind: "budget"; add: number }
  | { kind: "abort" }
  | { kind: "skill"; proposal: Proposal; approve: boolean; content: string; applyWorkflow: boolean };

/** Beside a choice: the note kept with it, the publish fields as edited (the rest go as shown),
 * and what an approve reviewed, so a single piece is named in the record. */
export type Extras = { note?: string; edits?: Record<string, string>; reviewed?: ArtifactFile[] };

/** A decision ready to post: `send` resolves to the refusal, or null once taken; `said` is it
 * in plain words, for the line that confirms it. */
export type Decision = { send: () => Promise<string | null>; said: string };

/** The request a choice at a gate posts. */
export function decision(gate: Gate, choice: Choice, { note = "", edits = {}, reviewed = [] }: Extras = {}): Decision {
  if (choice.kind === "abort") return { send: () => actOn(gate.run_id, "abort"), said: "You aborted the run" };
  if (choice.kind === "budget") {
    const total = Math.round(((gate.budget_usd ?? 0) + choice.add) * 100) / 100;
    return { send: () => setBudget(gate.run_id, total), said: `You raised the budget to ${money(total)}` };
  }
  if (choice.kind === "skill") {
    const { proposal, approve } = choice;
    return {
      send: async () => {
        const { error, response } = await api.POST("/api/proposals/{proposal_id}/decide", {
          params: { path: { proposal_id: proposal.id } },
          body: {
            approve,
            content: proposal.skill && proposal.skill.action !== "retired" ? choice.content : null,
            apply_workflow: approve && choice.applyWorkflow && proposal.workflow !== null,
            note,
          },
        });
        return refusal(error, response, "The decision was refused");
      },
      said: approve ? "You approved the proposal" : "You rejected the proposal",
    };
  }
  const body: Decide = { note };
  if (choice.kind === "pick") {
    body.choice = choice.choice;
    if (choice.artifact) body.artifact = choice.artifact;
  }
  // An approval or send-back of one piece names it, so the record says which.
  if (gate.kind === "approve" && reviewed.length === 1) body.artifact = reviewed[0]!.path;
  if (choice.kind === "send_back") body.send_back_to = choice.to;
  // What goes out is what was shown, edited or not; the decision records it.
  else if (gate.publish) {
    const shown = gate.publish.fields ?? [];
    body.edits = Object.fromEntries(shown.filter(editable).map((f) => [f.key, edits[f.key] ?? String(f.value ?? "")]));
    // The rest as shown; one that moved since (a relative schedule) is refused and shown again.
    body.seen = Object.fromEntries(shown.filter((f) => !editable(f)).map((f) => [f.key, f.value ?? null]));
  }
  const said =
    choice.kind === "send_back"
      ? `You sent it back to ${choice.to}`
      : choice.kind === "pick"
        ? `You picked "${choice.choice}"`
        : choice.kind === "confirm"
          ? "You confirmed it"
          : "You approved it";
  return { send: () => decide(gate.run_id, body), said };
}

/** A budget gate stays until the run resumes and can still be aborted, so answering it keeps it open. */
export const staysOpen = (gate: Gate) => gate.kind === "budget";
