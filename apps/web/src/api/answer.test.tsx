import { afterEach, expect, test, vi } from "vitest";

import { fakeFetch, gate } from "../test/fakes";
import { blocked, decision, type Proposal, sendBackTargets, staysOpen, topUpSuggestion } from "./answer";
import type { ArtifactFile } from "./events";

afterEach(() => vi.restoreAllMocks());

/** Answer through the shared code and return what it said and the one request it posted. */
async function post(...args: Parameters<typeof decision>) {
  const sent: { key: string; body: unknown }[] = [];
  const record = async (req: Request) => {
    sent.push({ key: `${req.method} ${new URL(req.url).pathname}`, body: req.method === "POST" && req.headers.get("content-type") ? await req.json() : null });
    return Response.json({});
  };
  fakeFetch({
    "POST /api/runs/run_1/decision": record,
    "POST /api/runs/run_1/budget": record,
    "POST /api/runs/run_1/abort": record,
    "POST /api/proposals/prop_1/decide": record,
  });
  const { send, said } = decision(...args);
  expect(await send()).toBeNull();
  expect(sent).toHaveLength(1);
  return { said, ...sent[0]! };
}

const piece = (path: string): ArtifactFile => ({ path, name: path, stage: "s0", type: "idea", title: path, fields: {} }) as unknown as ArtifactFile;

test("a pick posts the card it chose, or the typed name", async () => {
  expect(await post(gate("pick"), { kind: "pick", choice: "Hook first", artifact: "a/idea-1.md" }, { note: "go" })).toEqual({
    said: 'You picked "Hook first"',
    key: "POST /api/runs/run_1/decision",
    body: { note: "go", choice: "Hook first", artifact: "a/idea-1.md" },
  });
  expect((await post(gate("pick"), { kind: "pick", choice: "idea 1" })).body).toEqual({ note: "", choice: "idea 1" });
});

test("an approve names the one piece it reviewed, and a send back names the step", async () => {
  const g = gate("approve", { earlier_stages: ["research", "script"] });
  expect(await post(g, { kind: "approve" }, { reviewed: [piece("short.mp4")] })).toEqual({
    said: "You approved it",
    key: "POST /api/runs/run_1/decision",
    body: { note: "", artifact: "short.mp4" },
  });
  expect(await post(g, { kind: "send_back", to: "script" }, { note: "Hook sooner.", reviewed: [piece("a.md"), piece("b.md")] })).toEqual({
    said: "You sent it back to script",
    key: "POST /api/runs/run_1/decision",
    body: { note: "Hook sooner.", send_back_to: "script" },
  });
  // Nearest step first, and none once the revisions are used up.
  expect(sendBackTargets(g)).toEqual(["script", "research"]);
  expect(sendBackTargets({ ...g, send_backs_left: 0 })).toEqual([]);
});

test("a confirm before a publish posts what goes out, edited or as shown", async () => {
  const field = (key: string, value: unknown, editable: boolean) => ({ key, label: key, value, editable, hint: "" });
  const g = gate("confirm", {
    publish: { connector: "youtube", fields: [field("title", "Five hooks", true), field("description", "Ranked.", true), field("visibility", "public", false)], problems: [] },
  });
  expect(await post(g, { kind: "confirm" }, { edits: { title: "Six hooks" } })).toEqual({
    said: "You confirmed it",
    key: "POST /api/runs/run_1/decision",
    body: { note: "", edits: { title: "Six hooks", description: "Ranked." }, seen: { visibility: "public" } },
  });
  expect(blocked(g)).toBe(false);
  expect(blocked({ ...g, publish: { ...g.publish!, problems: ["needs a title"] } })).toBe(true);
});

test("a budget gate tops up on what it had, or aborts, and stays open", async () => {
  const g = gate("budget", { budget_usd: 5, spent_usd: 5.2 });
  expect(topUpSuggestion(g)).toBe(5);
  expect(await post(g, { kind: "budget", add: 2.5 })).toEqual({
    said: "You raised the budget to $7.50",
    key: "POST /api/runs/run_1/budget",
    body: { budget_usd: 7.5 },
  });
  expect(await post(g, { kind: "abort" })).toEqual({ said: "You aborted the run", key: "POST /api/runs/run_1/abort", body: null });
  expect(staysOpen(g)).toBe(true);
  expect(staysOpen(gate("approve"))).toBe(false);
});

test("a skill gate approves the edited skill with its workflow, or rejects it", async () => {
  const proposal = {
    id: "prop_1",
    skill: { action: "new", name: "hooks", content: "old", summary: "", evidence: [] },
    workflow: { summary: "", yaml: "", evidence: [] },
  } as unknown as Proposal;
  const g = gate("skill");
  expect(await post(g, { kind: "skill", proposal, approve: true, content: "new", applyWorkflow: true }, { note: "ok" })).toEqual({
    said: "You approved the proposal",
    key: "POST /api/proposals/prop_1/decide",
    body: { approve: true, content: "new", apply_workflow: true, note: "ok" },
  });
  expect(await post(g, { kind: "skill", proposal, approve: false, content: "new", applyWorkflow: true }, { note: "no" })).toMatchObject({
    said: "You rejected the proposal",
    body: { approve: false, apply_workflow: false, note: "no" },
  });
});

test("a refused decision comes back as the API's reason", async () => {
  fakeFetch({ "POST /api/runs/run_1/decision": () => Response.json({ detail: "The gate moved on." }, { status: 409 }) });
  expect(await decision(gate("confirm"), { kind: "confirm" }).send()).toBe("The gate moved on.");
});
