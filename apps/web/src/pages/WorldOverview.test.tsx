import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, gate, run } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = {
  id: "world_1",
  name: "Video",
  purpose: "Weekly shorts.",
  created_at: "2026-10-26T10:00:00+00:00",
};

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("shows the world, its figures, runs, gates and workflows, following the live stream", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await expect.element(screen.getByText("Weekly shorts.")).toBeVisible();
  await expect.element(screen.getByTestId("needs-you")).toMatchTextContent(/Nothing waits for you/);
  await expect.element(screen.getByText(/None yet. Start one/)).toBeVisible();
  await expect.element(screen.getByTestId("no-workflows")).toMatchTextContent(/recipe a run follows/);
  await expect
    .element(screen.getByRole("link", { name: "New workflow" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/new");
  // The sidebar carries navigation now; the delete control lives in Settings.
  expect(screen.getByRole("button", { name: "Delete world" }).elements()).toHaveLength(0);

  // Live only, beside the shell's one all-worlds stream.
  expect(FakeEventSource.open.map((s) => s.url).sort()).toEqual(["/api/events?since=now", "/api/worlds/world_1/events?since=now"]);
  FakeEventSource.emit("run.updated", { seq: 1, type: "run.updated", ts: video.created_at, payload: run("running", []) });
  await expect.element(screen.getByTestId("runs")).toMatchTextContent(/Render then pick.*running/);
});

test("the figures count today's top runs, their spend, the slots in use and first-try acceptance", async () => {
  const now = new Date().toISOString();
  const runs = [
    run("running", [], { id: "run_1", created_at: now, spent_usd: 1.5 }),
    run("waiting_gate", [], { id: "run_2", created_at: now, spent_usd: 2 }),
    run("running", [], { id: "run_3", created_at: now, spent_usd: 1, parent_run_id: "run_1" }), // in its parent's spend
    run("done", [], { id: "run_4", created_at: "2026-01-01T10:00:00+00:00", spent_usd: 9 }),
  ];
  const score = (judged: number, first_try: number) => ({
    run_id: "r", name: "Weekly short", workflow_version: 1, baseline: false, status: "done",
    created_at: now, judged, first_try, send_backs: 0, cost_usd: 1, duration_s: null, views: null,
  });
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: now };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ ...video, run_cap: 3 }),
    "GET /api/worlds/world_1/workflows": () => Response.json([weekly]),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/workflows/wf_1/scorecard": () =>
      Response.json({ workflow_id: "wf_1", world_id: "world_1", name: "Weekly short", runs: [score(3, 2), score(2, 2)] }),
  });
  const { screen } = await mountAt("/worlds/world_1");
  const figures = screen.getByRole("region", { name: "Figures" });
  await expect.element(figures).toMatchTextContent(/Runs today\s*2/);
  await expect.element(figures).toMatchTextContent(/Spent today\s*\$3\.50/);
  await expect.element(figures).toMatchTextContent(/Run slots in use\s*2 of 3/);
  await expect.element(figures).toMatchTextContent(/Accepted first try\s*4 of 5 judged/);
});

test("the runs table shows the newest five and links to all of them", async () => {
  const runs = Array.from({ length: 7 }, (_, i) => run("done", [], { id: `run_${i}`, name: `Run ${i}` }));
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("link", { name: "View all 7" })).toHaveAttribute("href", "/worlds/world_1/runs");
  await expect.element(screen.getByRole("link", { name: "Run 0" })).toHaveAttribute("href", "/worlds/world_1/runs/run_0");
  expect(screen.getByTestId("runs").getByRole("link").elements()).toHaveLength(5);
  expect(screen.getByRole("link", { name: "Run 5" }).elements()).toHaveLength(0);
});

test("each open gate of this world links to that gate in Needs you", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/gates": () =>
      Response.json([
        gate("pick", { stage_id: "stage_1" }),
        gate("approve", { stage_id: "stage_7", run_name: "Daily short" }),
        gate("confirm", { stage_id: "stage_9", world_id: "world_2" }),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByTestId("needs-you")).toMatchTextContent(/2 waiting/);
  const answers = screen.getByTestId("needs-you").getByRole("link", { name: "Answer" });
  await expect.element(answers.first()).toHaveAttribute("href", "/gates?world=world_1&gate=stage_1");
  await expect.element(answers.nth(1)).toHaveAttribute("href", "/gates?world=world_1&gate=stage_7");
  expect(answers.elements()).toHaveLength(2);
});

test("a replayed burst of run updates refetches the gates once, not once per event", async () => {
  const calls = fakeFetch({ "GET /api/worlds/world_1": () => Response.json(video) });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(1));
  for (let seq = 1; seq <= 50; seq++) {
    const payload = run("running", [], { id: `run_${seq}` });
    FakeEventSource.emit("run.updated", { seq, type: "run.updated", ts: video.created_at, payload });
  }
  await expect.element(screen.getByRole("link", { name: "View all 50" })).toBeVisible();
  await vi.waitFor(() => expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(2));
  await new Promise((r) => setTimeout(r, 300));
  expect(calls.filter((c) => c === "GET /api/gates")).toHaveLength(2);
});

test("lists the world's workflows and refreshes the list when one is saved", async () => {
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: video.created_at };
  let listed = [weekly];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json(listed),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByTestId("workflow-list")).toMatchTextContent(/Weekly short.*v1/);
  await expect
    .element(screen.getByRole("link", { name: "Weekly short" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/wf_1");

  listed = [{ ...weekly, name: "Daily short", current_version: 2 }];
  FakeEventSource.emit("workflow.saved", { seq: 2, type: "workflow.saved", ts: video.created_at });
  await expect.element(screen.getByTestId("workflow-list")).toMatchTextContent(/Daily short.*v2/);
});

test("an unknown world explains itself and links back", async () => {
  fakeFetch({});
  const { screen } = await mountAt("/worlds/world_nope");
  await expect.element(screen.getByRole("heading", { name: "No such world" })).toBeVisible();
  await expect.element(screen.getByRole("link", { name: "Back to your worlds." })).toBeVisible();
});

test("a server error is an alert, not 'No such world'", async () => {
  fakeFetch({ "GET /api/worlds/world_1": () => new Response("boom", { status: 500 }) });
  const { screen } = await mountAt("/worlds/world_1");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not load this world/);
  await expect.element(screen.getByText("No such world")).not.toBeInTheDocument();
});

test("starting a run of a workflow opens its timeline", async () => {
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: video.created_at };
  const started = {
    id: "run_1", world_id: "world_1", workflow_id: "wf_1", workflow_version: 1, name: "Weekly short",
    status: "queued", created_at: video.created_at, updated_at: video.created_at, stages: [],
  };
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([weekly]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/workflows/wf_1/runs": () => Response.json(started, { status: 201 }),
    "GET /api/runs/run_1": () => Response.json(started),
  });
  const { screen, router } = await mountAt("/worlds/world_1");
  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/Queued/);
  expect(calls).toContain("POST /api/workflows/wf_1/runs");
  expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_1");
});

test("a workflow run starts with the budget and worker count typed over the workflow's", async () => {
  const bodies: unknown[] = [];
  const weekly = { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 1, created_at: video.created_at };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([weekly]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/workflows/wf_1/runs": async (req) => {
      bodies.push(await req.json());
      return Response.json({ detail: "stop here" }, { status: 409 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1");
  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await expect.element(screen.getByRole("alert")).toBeVisible();
  await screen.getByRole("spinbutton", { name: "Run budget in USD" }).fill("3");
  await screen.getByRole("spinbutton", { name: "Workers per swarm stage" }).fill("2");
  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await screen.getByRole("button", { name: "Start a baseline of Weekly short" }).click();
  await expect.poll(() => bodies.length).toBe(3);
  expect(bodies).toEqual([
    { baseline: false },
    { baseline: false, budget_usd: 3, agents: 2 },
    { baseline: true, budget_usd: 3 }, // a baseline always runs one agent
  ]);
});

test("a workflow whose current version fails today's check is marked and its refusal shown", async () => {
  const stale = {
    id: "wf_1", world_id: "world_1", name: "Kids clickbait short", current_version: 1, created_at: video.created_at,
    violations: [{ rule: "connector", message: "render_video is not a connector of this world", stage: "render" }],
  };
  const fine = { ...stale, id: "wf_2", name: "Weekly short", violations: [] };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([stale, fine]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/workflows/wf_1/runs": () =>
      Response.json(
        { detail: "This version fails today's workflow check: render_video is not a connector of this world", violations: stale.violations },
        { status: 409 },
      ),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await expect
    .element(screen.getByTestId("fails-check-wf_1"))
    .toMatchTextContent(/Fails today's check: render_video is not a connector of this world/);
  expect(screen.getByTestId("fails-check-wf_2").elements()).toHaveLength(0);
  await screen.getByRole("button", { name: "Start a run of Kids clickbait short" }).click();
  await expect
    .element(screen.getByRole("alert"))
    .toMatchTextContent("Not started: This version fails today's workflow check: render_video is not a connector of this world.");
});

test("a typed goal starts an ad hoc run and opens its timeline", async () => {
  const bodies: unknown[] = [];
  const started = {
    id: "run_9", world_id: "world_1", workflow_id: null, workflow_version: null, name: "Find five hooks",
    status: "queued", created_at: video.created_at, updated_at: video.created_at, stages: [],
  };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/worlds/world_1/runs": async (req) => {
      bodies.push(await req.json());
      return Response.json(started, { status: 201 });
    },
    "GET /api/runs/run_9": () => Response.json(started),
  });
  const { screen, router } = await mountAt("/worlds/world_1");
  // Its own label, so it is not mistaken for a workflow's Start run.
  await expect.element(screen.getByRole("button", { name: "Start run" })).not.toBeInTheDocument();
  await screen.getByRole("textbox", { name: "Goal" }).fill("Find five hooks");
  await screen.getByRole("spinbutton", { name: "Budget in USD" }).fill("2.5");
  await screen.getByRole("button", { name: "Run goal" }).click();
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/Queued/);
  expect(bodies).toEqual([{ goal: "Find five hooks", done_when: "", baseline: false, budget_usd: 2.5 }]);
  expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_9");
  await expect.element(screen.getByRole("heading", { level: 1 })).toMatchTextContent(/Find five hooks.*ad hoc/);
});

test("a spent monthly budget is shown as the API's reason", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "POST /api/worlds/world_1/runs": () =>
      Response.json({ detail: "the world's monthly budget of $5.00 is spent" }, { status: 409 }),
  });
  const { screen } = await mountAt("/worlds/world_1");
  await screen.getByRole("textbox", { name: "Goal" }).fill("Anything");
  await screen.getByRole("button", { name: "Run goal" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Not started: the world's monthly budget of $5.00 is spent.");
});
