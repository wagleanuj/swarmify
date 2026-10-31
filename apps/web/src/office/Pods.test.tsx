import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import type { Run } from "../api/runs";
import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const world = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00", run_cap: 6, skin: { view: "office" } };
const recent = new Date(Date.now() - 60 * 60 * 1000).toISOString();
const old = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();

/** `n` stages, those before `at` done, `at` itself in `status`. */
function stages(n: number, at: number, type: string, status = "running", over = {}) {
  return Array.from({ length: n }, (_, i) =>
    stage(i, i === at ? "Render scenes" : `step ${i + 1}`, i === at ? type : "swarm", i < at ? "done" : i === at ? status : "pending", i === at ? over : {}),
  );
}

const weekly = run("running", stages(12, 6, "swarm"), { id: "run_14", name: "Weekly explainer #14", created_at: "2026-10-31T08:00:00Z" });
const long = run("running", stages(30, 6, "swarm"), { id: "run_15", name: "Long haul", created_at: "2026-10-31T08:01:00Z" });
const sending = run("running", stages(5, 3, "connector", "running", { calls: "youtube" }), { id: "run_16", name: "Short #3", created_at: "2026-10-31T08:02:00Z" });
const parent = run("running", stages(4, 1, "workflow", "running", { child: { id: "run_child", world_id: "world_1", name: "Research pack" } }), {
  id: "run_17",
  name: "Monthly digest",
  created_at: "2026-10-31T08:03:00Z",
});
const child = run("running", stages(4, 1, "swarm"), { id: "run_child", name: "Research pack", parent_run_id: "run_17" });
const retro = run("running", stages(3, 2, "retro"), { id: "run_18", name: "Retro run", created_at: "2026-10-31T08:04:00Z" });
const queued = (id: string, name: string, at: string) => run("queued", stages(3, 0, "swarm", "pending"), { id, name, created_at: at });
const finished = (id: string, name: string, status: Run["status"], at: string) => run(status, stages(3, 3, "swarm"), { id, name, updated_at: at });

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function office(runs: Run[], over: object = {}) {
  const score = (judged: number, first_try: number) => ({
    run_id: "r", name: "Weekly", workflow_version: 1, baseline: false, status: "done",
    created_at: recent, judged, first_try, send_backs: 0, cost_usd: 1, duration_s: null, outcomes: {},
  });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ ...world, ...over }),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/worlds/world_1/workflows": () => Response.json([{ id: "wf_1", world_id: "world_1", name: "Weekly", current_version: 1, created_at: recent }]),
    "GET /api/workflows/wf_1/scorecard": () => Response.json({ workflow_id: "wf_1", world_id: "world_1", name: "Weekly", runs: [score(3, 2), score(2, 1)] }),
  });
  return mountAt("/worlds/world_1/office");
}

test("each live top-level run is a pod with its stage n of m; spare pods stand empty; the sofa and shelf hold the rest", async () => {
  const { screen } = await office([
    weekly,
    long,
    child,
    queued("run_q2", "Changelog #11", "2026-10-31T09:30:00Z"),
    queued("run_q1", "Changelog #10", "2026-10-31T09:00:00Z"),
    finished("run_d1", "Weekly explainer #13", "done", recent),
    finished("run_d0", "Weekly explainer #12", "done", old),
  ]);
  const pods = screen.getByRole("list", { name: "Pods" });
  const pod = pods.getByRole("button", { name: "Weekly explainer #14, stage 7 of 12, Render scenes" });
  await expect.element(pod).toMatchTextContent(/Weekly explainer #14\s*Stage 7 of 12, Render scenes\s*Working/);
  // A 30-stage run reads the same as a 12-stage one: progress lives on the whiteboard.
  await expect.element(pods.getByRole("button", { name: "Long haul, stage 7 of 30, Render scenes" })).toBeVisible();
  // The child run shows on its parent's pod, not its own; of the four the cap of 6 leaves free,
  // two fill out the row of four.
  expect(pods.getByRole("button").elements()).toHaveLength(2);
  expect(pods.getByText("Free pod").elements()).toHaveLength(2);

  const sofa = screen.getByRole("group", { name: "Lobby sofa" }).getByRole("button");
  expect(sofa.elements().map((b) => b.getAttribute("aria-label"))).toEqual(["Changelog #10, queued", "Changelog #11, queued"]);
  const shelf = screen.getByRole("list", { name: "Shelf" }).getByRole("button");
  expect(shelf.elements().map((b) => b.getAttribute("aria-label"))).toEqual(["Weekly explainer #13, done"]);

  const board = screen.getByRole("complementary", { name: "Quest board" });
  expect(board.getByRole("button").elements().map((b) => b.getAttribute("aria-label"))).toEqual([
    "Weekly explainer #14, stage 7 of 12, Render scenes",
    "Long haul, stage 7 of 30, Render scenes",
    "Changelog #10, queued",
    "Changelog #11, queued",
    "Weekly explainer #13, done",
  ]);
  await expect.element(screen.getByTestId("office-first-try")).toMatchTextContent(/3 of 5 kept first try/);
});

test("a pod shows the type of its current stage", async () => {
  const gated = run("waiting_gate", stages(6, 2, "gate", "waiting"), { id: "run_19", name: "Pick a topic", created_at: "2026-10-31T08:05:00Z" });
  const paused = run("paused", stages(4, 1, "swarm", "pending"), { id: "run_20", name: "On hold", created_at: "2026-10-31T08:06:00Z" });
  const { screen } = await office([sending, parent, child, retro, gated, paused]);
  const pods = screen.getByRole("list", { name: "Pods" });
  await expect.element(pods.getByRole("button", { name: "Short #3, stage 4 of 5, Render scenes, sending to YouTube" })).toMatchTextContent(/Sending to YouTube/);
  await expect
    .element(pods.getByRole("button", { name: "Monthly digest, stage 2 of 4, Render scenes, child run Research pack, stage 2 of 4, Render scenes" }))
    .toMatchTextContent(/Child run Research pack, stage 2 of 4, Render scenes/);
  await expect.element(pods.getByRole("button", { name: "Retro run, stage 3 of 3, Render scenes, retro, learning from the run" })).toBeVisible();
  await expect.element(pods.getByRole("button", { name: "Pick a topic, stage 3 of 6, Render scenes, waiting on you" })).toMatchTextContent(/Waiting on you/);
  await expect.element(pods.getByRole("button", { name: "On hold, stage 2 of 4, Render scenes, paused" })).toMatchTextContent(/Paused/);
  expect(pods.getByText("Free pod").elements()).toHaveLength(1);
});

test("a run moving to a gate changes its pod without a reload", async () => {
  const { screen } = await office([weekly, queued("run_15", "Long haul", "2026-10-31T09:00:00Z")]);
  const sofa = screen.getByRole("group", { name: "Lobby sofa" });
  await expect.element(sofa.getByRole("button", { name: "Long haul, queued" })).toBeVisible();
  const pods = screen.getByRole("list", { name: "Pods" });
  await expect.element(pods.getByRole("button", { name: "Weekly explainer #14, stage 7 of 12, Render scenes" })).toMatchTextContent(/Working/);
  FakeEventSource.emit("run.updated", { payload: { ...weekly, status: "waiting_gate", stages: stages(12, 7, "gate", "waiting") } });
  const gated = pods.getByRole("button", { name: "Weekly explainer #14, stage 8 of 12, Render scenes, waiting on you" });
  await expect.element(gated).toMatchTextContent(/Waiting on you/);
  // A queued run that starts leaves the sofa for a free pod.
  expect(pods.getByText("Free pod").elements()).toHaveLength(3);
  FakeEventSource.emit("run.updated", { payload: { ...long, status: "running" } });
  await expect.element(pods.getByRole("button", { name: "Long haul, stage 7 of 30, Render scenes" })).toBeVisible();
  await expect.element(sofa.getByRole("button", { name: "Long haul, queued" })).not.toBeInTheDocument();
  expect(pods.getByText("Free pod").elements()).toHaveLength(2);
});

const done = finished("run_d1", "Weekly explainer #13", "done", recent);
test.each([
  ["list", "Pods", "Weekly explainer #14, stage 7 of 12, Render scenes", "run_14"],
  ["list", "Shelf", "Weekly explainer #13, done", "run_d1"],
  ["group", "Lobby sofa", "Changelog #10, queued", "run_q1"],
  ["complementary", "Quest board", "Weekly explainer #13, done", "run_d1"],
] as const)("a %s named %s opens the run's plain page", async (role, where, name, id) => {
  const { screen, router } = await office([weekly, done, queued("run_q1", "Changelog #10", recent)]);
  await screen.getByRole(role, { name: where }).getByRole("button", { name }).click();
  await expect.element(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  expect(router.state.location.pathname).toBe(`/worlds/world_1/runs/${id}`);
});

test("at 1280 by 720 with seven live runs every whiteboard reads in full, and the top bar keeps one row", async () => {
  await page.viewport(1280, 720);
  try {
    const more = [1, 2].map((n) => run("running", stages(3, 1, "swarm"), { id: `run_m${n}`, name: `More #${n}`, created_at: `2026-10-31T08:1${n}:00Z` }));
    const { screen } = await office([weekly, long, sending, parent, retro, ...more], { run_cap: 8 });
    const pods = screen.getByRole("list", { name: "Pods" });
    await expect.element(pods.getByRole("button", { name: /^More #2/ })).toBeVisible();
    const boards = pods.getByRole("button").elements();
    expect(boards).toHaveLength(7);
    for (const b of boards) {
      // Title, stage line and kind line each stand at least their font size high.
      for (const line of b.querySelectorAll("b, small, .office-kind")) {
        expect(line.getBoundingClientRect().height).toBeGreaterThanOrEqual(parseFloat(getComputedStyle(line).fontSize));
      }
      // The progress bar runs the board's width, not a tick.
      expect(b.querySelector(".office-progress")!.getBoundingClientRect().width).toBeGreaterThan(b.getBoundingClientRect().width * 0.8);
    }
    const bar = [...document.querySelector("header.office-bar")!.children].filter((c) => c.getClientRects().length);
    const top = bar[0]!.getBoundingClientRect().top;
    for (const c of bar) expect(c.getBoundingClientRect().top).toBeLessThan(top + 10);
  } finally {
    await page.viewport(1280, 800);
  }
});
