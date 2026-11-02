import { userEvent } from "vitest/browser";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Agent } from "../api/events";
import type { Gate, Run } from "../api/runs";
import { artifact, FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const world = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00", run_cap: 6, skin: { view: "office" } };
const ts = "2026-10-31T08:00:00Z";

const stages = (id: string, n: number, at: number) =>
  Array.from({ length: n }, (_, i) =>
    stage(i, i === at ? "Review" : `Step ${i + 1}`, i === at ? "gate" : "swarm", i < at ? "done" : i === at ? "waiting" : "pending", { id: `${id}_s${i}` }),
  );
const gatedRun = (id: string, name: string, n = 6, over = {}) =>
  run("waiting_gate", stages(id, n, 2), { id, name, spent_usd: 1.25, budget_usd: 5, ...over });

let seq = 0;
const agent = (name: string, role: Agent["role"], runId: string, at: number): Agent => ({
  id: `agent_${(seq += 1)}`, world_id: "world_1", run_id: runId, stage_id: `${runId}_s${at}`, name, role, status: "done",
  session_id: null, cost_usd: 0, created_at: ts, updated_at: ts,
});

const poses = (c: string) => ({ name: c, poses: { idle: ["idle.png"], talk: ["talk.png"], cheer: ["cheer.png"], stuck: ["stuck.png"] } });
const pack = {
  id: "kenney-toon",
  name: "Toon Characters",
  licence: "CC0",
  characters: { ada: poses("Ada"), bo: poses("Bo") },
  casting: { planner: { character: "ada", name: "Mira" }, judge: { character: "bo", name: "Rex" } },
};
const member = (name: string, role: string) => ({ name, role, version: 1, prompt: "", tools: null, updated_at: null });
const cast = [member("planner", "planner"), member("worker-1", "worker"), member("judge", "judge")];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

type Posted = { key: string; body: unknown };

/** The world's runs, agents and open gates; every answer posted is recorded and closes its gate,
 * except a budget top-up: that gate stays until the run resumes. */
function world1(runs: Run[], agents: Agent[], gates: Gate[], extra: Record<string, (req: Request) => Response | Promise<Response>> = {}) {
  const posted: Posted[] = [];
  const close = (runId: string, keep = false) => async (req: Request) => {
    posted.push({ key: `${req.method} ${new URL(req.url).pathname}`, body: req.headers.get("content-type") ? await req.json() : null });
    const at = gates.findIndex((g) => g.run_id === runId);
    if (at >= 0 && !keep) gates.splice(at, 1);
    return Response.json({});
  };
  const routes: Record<string, (req: Request) => Response | Promise<Response>> = {
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/sprite-packs": () => Response.json([pack]),
    "GET /api/gates": () => Response.json(gates),
    ...extra,
  };
  for (const r of runs) {
    routes[`GET /api/runs/${r.id}/agents`] = () => Response.json(agents.filter((a) => a.run_id === r.id));
    routes[`GET /api/runs/${r.id}/artifacts`] = () => Response.json([]);
    for (const what of ["decision", "budget", "abort"]) routes[`POST /api/runs/${r.id}/${what}`] = close(r.id, what === "budget");
  }
  routes["POST /api/proposals/prop_1/decide"] ??= close("run_a");
  fakeFetch({ ...routes, ...extra });
  return posted;
}

// Three gates of this world opened out of order, and one of another world.
const judged = gatedRun("run_a", "Launch thread");
const planned = gatedRun("run_b", "Weekly explainer #13", 12);
const bare = gatedRun("run_c", "Competitor scan");
const gates = () => [
  gate("approve", { run_id: "run_b", run_name: "Weekly explainer #13", stage_id: "run_b_s2", stage_index: 2, ask: "Top it up?", opened_at: "2026-10-31T09:02:00Z" }),
  gate("approve", { run_id: "run_a", run_name: "Launch thread", stage_id: "run_a_s2", stage_index: 2, ask: "Ship it to Publish?", opened_at: "2026-10-31T09:01:00Z" }),
  gate("pick", { run_id: "run_x", world_id: "world_2", run_name: "Elsewhere", stage_id: "run_x_s1", opened_at: "2026-10-31T09:00:00Z" }),
  gate("confirm", { run_id: "run_c", run_name: "Competitor scan", stage_id: "run_c_s2", stage_index: 2, ask: "Post it?", opened_at: "2026-10-31T09:03:00Z" }),
];
const agents = [
  // The judge judged the stage before run_a's gate; run_b's judge judged an older one, so its planner asks.
  agent("judge", "judge", "run_a", 1),
  agent("planner", "planner", "run_a", 0),
  agent("judge", "judge", "run_b", 0),
  agent("planner", "planner", "run_b", 0),
];

test("the askers line up at Your office oldest first, the right one per gate, with the count", async () => {
  world1([judged, planned, bare], agents, gates());
  const { screen, router } = await mountAt("/worlds/world_1/office");
  const line = screen.getByRole("list", { name: "Line at Your office" });
  await expect.element(line.getByRole("button", { name: "Rex asks about Launch thread: Ship it to Publish?" })).toBeVisible();
  expect(line.getByRole("button").elements().map((b) => b.getAttribute("aria-label"))).toEqual([
    "Rex asks about Launch thread: Ship it to Publish?",
    "Mira asks about Weekly explainer #13: Top it up?",
    // No agents of its own: the cast's planner stands in.
    "Mira asks about Competitor scan: Post it?",
  ]);
  await expect.element(screen.getByTestId("office-waiting")).toHaveTextContent("3 decisions waiting on you");

  // Someone in line opens Talk with their gate; Your office opens it at the oldest.
  await line.getByRole("button", { name: /^Mira asks about Weekly/ }).click();
  await expect.element(screen.getByRole("heading", { name: "Mira Planner", level: 1 })).toBeVisible();
  await expect.element(screen.getByText("Top it up?")).toBeVisible();
  expect(router.state.location.pathname).toBe("/worlds/world_1/office/talk");
  expect(screen.getByRole("navigation", { name: "Main" }).elements()).toHaveLength(0);
  // Talk's top bar leads to Crew as the office's does.
  await expect.element(screen.getByRole("banner", { name: "Talk" }).getByRole("link", { name: "Crew" })).toBeVisible();
  await screen.getByRole("link", { name: "Back to the office" }).click();
  await screen.getByRole("button", { name: "Your office, 3 waiting" }).click();
  await expect.element(screen.getByText("Ship it to Publish?")).toBeVisible();
  await expect.element(screen.getByRole("img", { name: "Rex, Judge, asking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/talk.png");
  await expect.element(screen.getByTestId("talk-place")).toHaveTextContent("1 of 3 waiting at Your office");
  // The run beside the ask, with a link to its plain page.
  const ctx = screen.getByRole("list", { name: "The run" });
  await expect.element(ctx).toMatchTextContent(/Launch thread.*stage 3 of 6, approve.*Spent \$1\.25 of \$5\.00/);
  await expect.element(ctx.getByRole("link", { name: "Launch thread" })).toHaveAttribute("href", "/worlds/world_1/runs/run_a?stage=run_a_s2");
});

test("a gate in a child run is asked by that run's agents, at the door as in Talk", async () => {
  const child = gatedRun("run_k", "Launch thread, cut 2", 6, { parent_run_id: "run_a", status: "waiting_gate" });
  const parent = run("running", stages("run_a", 6, 2), { id: "run_a", name: "Launch thread" });
  world1([parent, child], [agent("judge", "judge", "run_k", 1)], [
    gate("approve", { run_id: "run_k", run_name: "Launch thread, cut 2", stage_id: "run_k_s2", stage_index: 2, ask: "Keep cut 2?" }),
  ]);
  const { screen } = await mountAt("/worlds/world_1/office");
  const line = screen.getByRole("list", { name: "Line at Your office" });
  await expect.element(line.getByRole("button", { name: "Rex asks about Launch thread, cut 2: Keep cut 2?" })).toBeVisible();
  await line.getByRole("button", { name: /^Rex asks/ }).click();
  await expect.element(screen.getByRole("heading", { name: "Rex Judge", level: 1 })).toBeVisible();
});

test("an answer posts the gate's decision, the asker reacts and the next steps up; then All clear", async () => {
  const posted = world1([judged, planned], agents, gates().slice(0, 2));
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  await expect.element(screen.getByText("Ship it to Publish?")).toBeVisible();
  // Number keys answer: 1 approves, 2 sends back to the step before.
  await screen.getByLabelText("Note (optional)").fill("Tighter ending.");
  (document.activeElement as HTMLElement).blur();
  await userEvent.keyboard("2");
  await expect.element(screen.getByText("Top it up?")).toBeVisible();
  expect(posted).toEqual([{ key: "POST /api/runs/run_a/decision", body: { note: "Tighter ending.", send_back_to: "research" } }]);
  await expect.element(screen.getByRole("status")).toHaveTextContent("You sent it back to research.");
  await expect.element(screen.getByRole("img", { name: "Rex slumps" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/stuck.png");
  await expect.element(screen.getByRole("heading", { name: "Mira Planner", level: 1 })).toBeVisible();

  await screen.getByRole("button", { name: "1 Approve" }).click();
  await expect.element(screen.getByRole("heading", { name: "All clear" })).toBeVisible();
  expect(posted[1]).toEqual({ key: "POST /api/runs/run_b/decision", body: { note: "" } });
  await expect.element(screen.getByRole("img", { name: "Mira cheers" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/cheer.png");
  await expect.element(screen.getByText(/Nobody needs you\. 0 runs are moving/)).toBeVisible();
});

test("a publish gate shows what goes out before any choice and posts it as shown or edited", async () => {
  const field = (key: string, value: unknown, editable: boolean) => ({ key, label: key, value, editable, hint: "" });
  const publish = { connector: "youtube", fields: [field("title", "Five hooks", true), field("visibility", "public", false)], problems: [] };
  const posted = world1([bare], [], [gate("confirm", { run_id: "run_c", run_name: "Competitor scan", stage_index: 2, publish })]);
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  const out = screen.getByRole("region", { name: "What goes out" });
  await expect.element(out).toMatchTextContent(/Where\s*YouTube.*visibility\s*public/);
  const choices = screen.getByRole("list", { name: "Choices" }).element();
  expect(out.element().compareDocumentPosition(choices) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await screen.getByLabelText("title").fill("Six hooks");
  await screen.getByRole("button", { name: "1 Confirm and post" }).click();
  await expect.element(screen.getByRole("heading", { name: "All clear" })).toBeVisible();
  expect(posted).toEqual([
    { key: "POST /api/runs/run_c/decision", body: { note: "", edits: { title: "Six hooks" }, seen: { visibility: "public" } } },
  ]);
});

test("a post that cannot go out cannot be confirmed", async () => {
  const publish = { connector: "youtube", fields: [], problems: ["no title"] };
  world1([bare], [], [gate("confirm", { run_id: "run_c", stage_index: 2, publish })]);
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/This will not post:\s*no title/);
  await expect.element(screen.getByRole("button", { name: "1 Confirm and post" })).toBeDisabled();
});

test("a post blocked by an editable field can be confirmed once that field is edited", async () => {
  const publish = { connector: "youtube", fields: [{ key: "hashtags", label: "hashtags", value: "#a #b #c #d #e #f", editable: true, hint: "" }], problems: ["6 hashtags, at most 5"] };
  const posted = world1([bare], [], [gate("confirm", { run_id: "run_c", stage_index: 2, publish })]);
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  await expect.element(screen.getByRole("button", { name: "1 Confirm and post" })).toBeDisabled();
  await screen.getByLabelText("hashtags").fill("#a #b #c");
  await screen.getByRole("button", { name: "1 Confirm and post" }).click();
  await vi.waitFor(() => expect(posted).toEqual([{ key: "POST /api/runs/run_c/decision", body: { note: "", edits: { hashtags: "#a #b #c" }, seen: {} } }]));
});

test("a pick posts the card chosen, or the typed name when no cards reached the gate", async () => {
  const cards = [artifact({ path: "a/idea-2.md", title: "Hook last", fields: { rank: 2 } }), artifact({ path: "a/idea-1.md", title: "Hook first", fields: { rank: 1 } })];
  const posted = world1([judged, bare], [], [
    gate("pick", { run_id: "run_a", stage_id: "run_a_s2", stage_index: 2, opened_at: "2026-10-31T09:01:00Z" }),
    gate("pick", { run_id: "run_c", stage_id: "run_c_s2", stage_index: 2, opened_at: "2026-10-31T09:02:00Z" }),
  ], { "GET /api/runs/run_a/artifacts": () => Response.json(cards) });
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  // Best ranked first.
  await expect.element(screen.getByRole("button", { name: "1 Pick Hook first" })).toBeVisible();
  await userEvent.keyboard("2");
  await expect.element(screen.getByLabelText("Your pick")).toBeVisible();
  expect(posted).toEqual([{ key: "POST /api/runs/run_a/decision", body: { note: "", choice: "Hook last", artifact: "a/idea-2.md" } }]);
  // An empty pick does not go.
  await screen.getByRole("button", { name: "1 Go with my pick" }).click();
  expect(posted).toHaveLength(1);
  await screen.getByLabelText("Your pick").fill("idea 3");
  await screen.getByRole("button", { name: "1 Go with my pick" }).click();
  await expect.element(screen.getByRole("heading", { name: "All clear" })).toBeVisible();
  expect(posted[1]).toEqual({ key: "POST /api/runs/run_c/decision", body: { note: "", choice: "idea 3" } });
});

test("a budget gate tops up or aborts, as the plain page does", async () => {
  const budget = (runId: string, at: string) =>
    gate("budget", { run_id: runId, stage_id: `${runId}_s2`, stage_index: 2, budget_usd: 5, spent_usd: 5.2, opened_at: at });
  const open = [budget("run_a", "2026-10-31T09:01:00Z"), budget("run_c", "2026-10-31T09:02:00Z")];
  const posted = world1([judged, bare], [], open);
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  await expect.element(screen.getByRole("list", { name: "The run" })).toMatchTextContent(/Spent \$5\.20 of \$5\.00/);
  await screen.getByLabelText("Add (US dollars)").fill("2");
  await screen.getByRole("button", { name: "1 Add money and go on" }).click();
  await expect.element(screen.getByRole("status")).toHaveTextContent("You raised the budget to $7.00.");
  expect(posted[0]).toEqual({ key: "POST /api/runs/run_a/budget", body: { budget_usd: 7 } });
  // The budget gate stays until the run resumes: its ask is still up after the top-up.
  await expect.element(screen.getByTestId("talk-place")).toHaveTextContent("1 of 2 waiting at Your office");
  await expect.element(screen.getByRole("heading", { name: "Mira Planner", level: 1 })).toBeVisible();
  await expect.element(screen.getByRole("list", { name: "The run" }).getByRole("link")).toHaveAttribute("href", "/worlds/world_1/runs/run_a?stage=run_a_s2");
  // The run resumes: the next reload no longer lists it.
  open.splice(0, 1);
  FakeEventSource.emit("run.updated", { payload: { ...judged, status: "running" } });
  await expect.element(screen.getByTestId("talk-place")).toHaveTextContent("1 of 1 waiting at Your office");
  await screen.getByRole("button", { name: "2 Abort the run" }).click();
  await expect.element(screen.getByRole("heading", { name: "All clear" })).toBeVisible();
  expect(posted[1]).toEqual({ key: "POST /api/runs/run_c/abort", body: null });
});

test("a skill gate shows the proposal, approves it as edited, and saving the workflow is a choice", async () => {
  const proposal = {
    id: "prop_1", world_id: "world_1", run_id: "run_a", stage_id: "run_a_s2", status: "open",
    skill: { action: "new", name: "hooks", content: "# Hooks", summary: "Hook first", evidence: ["3 of 4 kept"] },
    workflow: { summary: "Ask for hooks up front", evidence: [], yaml: "name: hooks-first" },
  };
  const posted = world1([judged], [], [gate("skill", { run_id: "run_a", stage_index: 2 })], {
    "GET /api/runs/run_a/proposal": () => Response.json(proposal),
  });
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  const skill = screen.getByRole("region", { name: "Skill change" });
  await expect.element(skill).toMatchTextContent(/new skill hooks: Hook first.*Evidence: 3 of 4 kept/);
  const flow = screen.getByRole("region", { name: "Workflow change" });
  await expect.element(flow).toMatchTextContent(/Workflow change: Ask for hooks up front.*name: hooks-first/);
  await expect.element(flow.getByLabelText("Save it as the workflow's next version when approving")).toBeChecked();
  await screen.getByLabelText(/^SKILL\.md \(edit before approving\)/).fill("# Hooks, first");
  await flow.getByLabelText("Save it as the workflow's next version when approving").click();
  await screen.getByRole("button", { name: "1 Approve" }).click();
  await expect.element(screen.getByRole("heading", { name: "All clear" })).toBeVisible();
  expect(posted).toEqual([
    { key: "POST /api/proposals/prop_1/decide", body: { approve: true, content: "# Hooks, first", apply_workflow: false, note: "" } },
  ]);
});

test("a refused answer says why and keeps the asker up", async () => {
  world1([judged], agents, [gate("approve", { run_id: "run_a", stage_index: 2 })], {
    "POST /api/runs/run_a/decision": () => Response.json({ detail: "already sent back 2 times; approve or abort" }, { status: 409 }),
  });
  const { screen } = await mountAt("/worlds/world_1/office/talk");
  await screen.getByRole("button", { name: "1 Approve" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("already sent back 2 times; approve or abort");
  await expect.element(screen.getByRole("heading", { name: "Rex Judge", level: 1 })).toBeVisible();
});
