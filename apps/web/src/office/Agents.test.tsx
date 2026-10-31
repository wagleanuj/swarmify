import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import type { Agent } from "../api/events";
import type { Gate, Run } from "../api/runs";
import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const world = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00", run_cap: 6, skin: { view: "office" } };
const recent = new Date(Date.now() - 60 * 60 * 1000).toISOString();

/** `n` stages of run `id`, those before `at` done, `at` itself in `status` and of `type`. */
const stages = (id: string, n: number, at: number, type = "swarm", status = "running") =>
  Array.from({ length: n }, (_, i) =>
    stage(i, i === at ? "Render scenes" : `Step ${i + 1}`, i === at ? type : "swarm", i < at ? "done" : i === at ? status : "pending", { id: `${id}_s${i}` }),
  );
const weekly = run("running", stages("run_14", 12, 6), { id: "run_14", name: "Weekly explainer #14", created_at: "2026-10-31T08:00:00Z" });
const long = run("running", stages("run_15", 8, 2), { id: "run_15", name: "Long haul", created_at: "2026-10-31T08:01:00Z" });
const gated = run("waiting_gate", stages("run_19", 6, 2, "gate", "waiting"), { id: "run_19", name: "Pick a topic", created_at: "2026-10-31T08:02:00Z" });
const shipped = run("done", stages("run_d1", 3, 3), { id: "run_d1", name: "Weekly explainer #13", updated_at: recent });
const paused = run("paused", stages("run_20", 4, 1), { id: "run_20", name: "On hold", created_at: "2026-10-31T08:03:00Z" });
const retro = run("running", stages("run_21", 4, 3, "retro"), { id: "run_21", name: "Wrap up", created_at: "2026-10-31T08:04:00Z" });
const broke = run("failed", stages("run_d2", 3, 3), { id: "run_d2", name: "Weekly explainer #12", updated_at: recent });

let seq = 0;
function agent(name: string, role: Agent["role"], runId: string, stageAt: number, status: Agent["status"] = "working"): Agent {
  seq += 1;
  return {
    id: `agent_${seq}`, world_id: "world_1", run_id: runId, stage_id: `${runId}_s${stageAt}`, name, role, status,
    session_id: null, cost_usd: 0, created_at: recent, updated_at: new Date(Date.now() - 1000 * (100 - seq)).toISOString(),
  };
}

const poses = (c: string) => ({
  name: c,
  poses: { idle: ["idle.png"], think: ["think.png"], work: ["work0.png", "work1.png"], walk: ["walk0.png"], talk: ["talk.png"], cheer: ["cheer.png"], stuck: ["stuck.png"], back: ["back.png"] },
});
const pack = {
  id: "kenney-toon",
  name: "Toon Characters",
  licence: "CC0",
  characters: { ada: poses("Ada"), bo: poses("Bo") },
  casting: { planner: { character: "ada", name: "Mira" }, judge: { character: "bo", name: "Rex" }, tester: { character: "bo", name: "Tess" } },
};
const member = (name: string, role: string) => ({ name, role, version: 1, prompt: "", tools: null, updated_at: null });
const cast = [member("planner", "planner"), member("worker-1", "worker"), member("worker-2", "worker"), member("judge", "judge"), member("qa", "tester"), member("budget", "budget")];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** The office over `runs`, with the agents each run's agents read answers and `gates` open. */
function office(runs: Run[], agents: Agent[], gates: Gate[] = []) {
  const routes: Record<string, () => Response> = {
    "GET /api/gates": () => Response.json(gates),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/sprite-packs": () => Response.json([pack]),
  };
  for (const r of runs) routes[`GET /api/runs/${r.id}/agents`] = () => Response.json(agents.filter((a) => a.run_id === r.id));
  fakeFetch(routes);
  return mountAt("/worlds/world_1/office");
}

const crewOf = (screen: Awaited<ReturnType<typeof office>>["screen"], name: string) => screen.getByRole("list", { name: `Crew of ${name}` });

test("each pod's crew stands in the pose its data says; the rest of the cast waits off the floor", async () => {
  const { screen } = await office(
    [weekly, long, gated, paused, retro, shipped, broke],
    [
      agent("planner", "planner", "run_14", 6),
      agent("worker-1", "worker", "run_14", 6),
      agent("worker-2", "worker", "run_14", 6, "failed"),
      agent("judge", "judge", "run_14", 5, "done"),
      // The same cast member working in a second run is drawn there too.
      agent("worker-1", "worker", "run_15", 2),
      // At a gate the desks empty and the judge of the stage before asks.
      agent("worker-1", "worker", "run_19", 1, "done"),
      agent("judge", "judge", "run_19", 1, "done"),
      // Everyone in a paused run is stuck; on the retro stage they work at the whiteboard.
      agent("worker-1", "worker", "run_20", 1),
      agent("worker-2", "worker", "run_21", 3),
      agent("qa", "worker", "run_d1", 2, "done"),
      agent("budget", "worker", "run_d2", 2, "failed"),
    ],
    [gate("pick", { run_id: "run_19", run_name: "Pick a topic", stage_id: "run_19_s2", stage_index: 2 })],
  );
  const weeklyCrew = crewOf(screen, "Weekly explainer #14");
  await expect.element(weeklyCrew.getByRole("img", { name: "Mira, Planner, thinking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/think.png");
  await expect.element(weeklyCrew.getByRole("img", { name: "Worker 1, working" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/work0.png");
  await expect.element(weeklyCrew.getByRole("img", { name: "Worker 2, stuck" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/stuck.png");
  expect(weeklyCrew.getByRole("img").elements()).toHaveLength(3);
  await expect.element(crewOf(screen, "Long haul").getByRole("img", { name: "Worker 1, working" })).toBeVisible();

  // At a gate the desks empty; the judge of the stage before waits at Your office to ask.
  const line = screen.getByRole("list", { name: "Line at Your office" });
  await expect.element(line.getByRole("button", { name: "Rex asks about Pick a topic: Pick one idea." })).toBeVisible();
  expect(crewOf(screen, "Pick a topic").elements()).toHaveLength(0);
  await expect.element(crewOf(screen, "On hold").getByRole("img", { name: "Worker 1, stuck" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/stuck.png");
  await expect
    .element(crewOf(screen, "Wrap up").getByRole("img", { name: "Worker 2, at the whiteboard" }))
    .toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/back.png");

  // Off the floor: shipped after a run that is done, stuck after one that failed, else idle.
  const bench = screen.getByRole("list", { name: "The cast" });
  await expect.element(bench.getByRole("img", { name: "Tess, qa, shipped" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/cheer.png");
  await expect.element(bench.getByRole("img", { name: "Budget, stuck" })).toBeVisible();
  expect(bench.getByRole("img").elements().map((i) => i.getAttribute("alt"))).toEqual(["Tess, qa, shipped", "Budget, stuck", "Retro"]);
});

test("one speech bubble per pod, from its crew's latest log line", async () => {
  const s6 = "run_14_s6";
  const { screen } = await office([weekly, long], [agent("planner", "planner", "run_14", 6), agent("worker-1", "worker", "run_14", 6), agent("worker-2", "worker", "run_15", 2)]);
  const pod = crewOf(screen, "Weekly explainer #14");
  await expect.element(pod.getByRole("img", { name: "Worker 1, working" })).toBeVisible();
  const log = (agentName: string, stageId: string, detail: string, kind = "tool") =>
    FakeEventSource.emit("agent.logged", { payload: { seq: 1, agent: agentName, stage_id: stageId, kind, ts: recent, detail } });

  log("worker-1", s6, "Reading the brief");
  await expect.element(pod.getByText("Reading the brief")).toBeVisible();
  log("planner", s6, "Splitting scenes into tasks\nand more");
  await expect.element(pod.getByText("Splitting scenes into tasks")).toBeVisible();
  expect(pod.getByText("Reading the brief").elements()).toHaveLength(0);
  // Session bookkeeping says nothing; another pod's line shows at that pod.
  log("planner", s6, "session started", "session");
  log("worker-2", "run_15_s2", "Rendering scene 3");
  await expect.element(crewOf(screen, "Long haul").getByText("Rendering scene 3")).toBeVisible();
  await expect.element(pod.getByText("Splitting scenes into tasks")).toBeVisible();
});

test("an artifact made pops up at its own pod and is called out on the radio", async () => {
  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: false } as MediaQueryList);
  const { screen } = await office([weekly, long], [agent("worker-1", "worker", "run_15", 2)]);
  const pods = screen.getByRole("list", { name: "Pods" });
  await expect.element(crewOf(screen, "Long haul")).toBeVisible();
  FakeEventSource.emit("artifact.created", { payload: { type: "script", path: "scripts/script.md", author: "worker-1", stage_id: "run_15_s2" } });
  const pod = pods.getByRole("listitem").filter({ hasText: "Long haul" });
  await expect.element(pod.getByText("script.md")).toBeVisible();
  expect(pods.getByRole("listitem").filter({ hasText: "Weekly explainer #14" }).getByText("script.md").elements()).toHaveLength(0);
  await expect.element(screen.getByRole("region", { name: "Radio" })).toMatchTextContent(/\d\d:\d\d Worker 1 made script\.md/);
  // It rises over its pod, never over the whiteboard's text, and is gone once it has risen.
  const board = pods.getByRole("button", { name: /^Long haul/ }).element().getBoundingClientRect();
  expect(pod.getByText("script.md").element().getBoundingClientRect().bottom).toBeLessThanOrEqual(board.top);
  await expect.poll(() => pod.getByText("script.md").elements(), { timeout: 5000 }).toHaveLength(0);
});

test("an agent.updated event moves the agent, and the radio says so in the cast's names", async () => {
  const worker = agent("worker-1", "worker", "run_14", 6);
  const planner = agent("planner", "planner", "run_14", 6);
  const gates: Gate[] = [];
  const { screen } = await office([weekly], [worker, planner], gates);
  const radio = screen.getByRole("region", { name: "Radio" });
  await expect.element(radio).toMatchTextContent(/Quiet for now\./);
  const pod = crewOf(screen, "Weekly explainer #14");
  await expect.element(pod.getByRole("img", { name: "Worker 1, working" })).toBeVisible();

  // Done with the stage, the worker leaves the desks for the rest of the cast.
  FakeEventSource.emit("agent.updated", { payload: { ...worker, status: "done" } });
  await expect.element(screen.getByRole("list", { name: "The cast" }).getByRole("img", { name: "Worker 1" })).toBeVisible();
  expect(pod.getByRole("img", { name: "Worker 1, working" }).elements()).toHaveLength(0);
  await expect.element(radio).toMatchTextContent(/Worker 1 finished Render scenes/);

  // The run moves on to a gate: the desks empty and the planner joins the line to ask.
  FakeEventSource.emit("agent.updated", { payload: { ...planner, status: "done" } });
  gates.push(gate("approve", { run_id: "run_14", run_name: "Weekly explainer #14", stage_id: "run_14_s7", stage_index: 7 }));
  FakeEventSource.emit("run.updated", { payload: { ...weekly, status: "waiting_gate", stages: stages("run_14", 12, 7, "gate", "waiting") } });
  const line = screen.getByRole("list", { name: "Line at Your office" });
  await expect.element(line.getByRole("button", { name: /^Mira asks about Weekly explainer #14/ })).toBeVisible();
  expect(pod.elements()).toHaveLength(0);
  await expect.element(screen.getByRole("button", { name: "Your office, 1 waiting" })).toBeVisible();
  expect(radio.getByRole("listitem").elements().map((li) => li.textContent?.slice(6))).toEqual(["Mira finished Render scenes", "Worker 1 finished Render scenes"]);
});

test("with reduced motion every character holds its pose; with motion an arriving agent walks in", async () => {
  const { screen } = await office([weekly], [agent("worker-1", "worker", "run_14", 6)]);
  const pod = crewOf(screen, "Weekly explainer #14");
  const worker = pod.getByRole("img", { name: "Worker 1, working" });
  await expect.element(worker).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/work0.png");
  await new Promise((done) => setTimeout(done, 400));
  await expect.element(worker).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/work0.png");

  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: false } as MediaQueryList);
  FakeEventSource.emit("agent.updated", { payload: agent("worker-2", "worker", "run_14", 6) });
  await expect.element(pod.getByRole("img", { name: "Worker 2, walking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/walk0.png");
  await expect.element(pod.getByRole("img", { name: "Worker 2, working" }), { timeout: 3000 }).toBeVisible();
});

test("a child run's artifact pops up at its parent's pod; a connector's is called out by its own name", async () => {
  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: false } as MediaQueryList);
  const parentStages = stages("run_30", 3, 1, "workflow").map((s, i) => (i === 1 ? { ...s, child: { id: "run_31", world_id: "world_1", name: "Research pack" } } : s));
  const parent = run("running", parentStages, { id: "run_30", name: "Big job", created_at: "2026-10-31T08:00:00Z" });
  const child = run("running", stages("run_31", 2, 0), { id: "run_31", name: "Research pack", parent_run_id: "run_30" });
  const { screen } = await office([parent, child], []);
  const pods = screen.getByRole("list", { name: "Pods" });
  const pod = pods.getByRole("listitem").filter({ hasText: "Big job" });
  await expect.element(pod).toBeVisible();
  FakeEventSource.emit("artifact.created", { payload: { type: "video", path: "out/video.mp4", author: "remotion_render", stage_id: "run_31_s0" } });
  await expect.element(pod.getByText("video.mp4")).toBeVisible();
  await expect.element(screen.getByRole("region", { name: "Radio" })).toMatchTextContent(/\d\d:\d\d remotion_render made video\.mp4/);
});

test("with reduced motion an artifact made does not pop up; the radio still says so", async () => {
  const { screen } = await office([long], [agent("worker-1", "worker", "run_15", 2)]);
  await expect.element(crewOf(screen, "Long haul")).toBeVisible();
  FakeEventSource.emit("artifact.created", { payload: { type: "script", path: "scripts/script.md", author: "worker-1", stage_id: "run_15_s2" } });
  await expect.element(screen.getByRole("region", { name: "Radio" })).toMatchTextContent(/Worker 1 made script\.md/);
  expect(screen.getByRole("list", { name: "Pods" }).getByText("script.md").elements()).toHaveLength(0);
});

test("at a gate of a run with no agents the cast's planner stands in to ask", async () => {
  const { screen } = await office([gated], [], [gate("pick", { run_id: "run_19", run_name: "Pick a topic", stage_index: 2 })]);
  const line = screen.getByRole("list", { name: "Line at Your office" });
  await expect.element(line.getByRole("button", { name: /^Mira asks about Pick a topic/ })).toBeVisible();
  expect(screen.getByRole("list", { name: "The cast" }).getByRole("img", { name: /Mira/ }).elements()).toHaveLength(0);
});

test("updates in one batch are told once, and a bubble leaves with its speaker", async () => {
  const worker = agent("worker-1", "worker", "run_14", 6);
  const { screen } = await office([weekly], [worker]);
  const pod = crewOf(screen, "Weekly explainer #14");
  await expect.element(pod.getByRole("img", { name: "Worker 1, working" })).toBeVisible();
  FakeEventSource.emit("agent.logged", { payload: { seq: 1, agent: "worker-1", stage_id: "run_14_s6", kind: "tool", ts: recent, detail: "Reading the brief" } });
  await expect.element(pod.getByText("Reading the brief")).toBeVisible();

  FakeEventSource.emit("agent.updated", { payload: { ...worker, status: "done" } });
  FakeEventSource.emit("agent.updated", { payload: { ...worker, status: "done" } });
  const radio = screen.getByRole("region", { name: "Radio" });
  await expect.element(radio).toMatchTextContent(/Worker 1 finished Render scenes/);
  expect(radio.getByRole("listitem").elements()).toHaveLength(1);

  FakeEventSource.emit("agent.updated", { payload: { ...worker, status: "working" } });
  await expect.element(pod.getByRole("img", { name: "Worker 1, working" })).toBeVisible();
  expect(pod.getByText("Reading the brief").elements()).toHaveLength(0);
});

test("a pod's crew stands under its whiteboard, never over it", async () => {
  // Only the empty top of a frame (about a quarter of it) reaches over the board.
  const { screen } = await office([weekly], [agent("planner", "planner", "run_14", 6), agent("worker-1", "worker", "run_14", 6), agent("worker-2", "worker", "run_14", 6)]);
  const crew = crewOf(screen, "Weekly explainer #14");
  await expect.element(crew.getByRole("img", { name: "Worker 2, working" })).toBeVisible();
  const board = screen.getByRole("list", { name: "Pods" }).getByRole("button", { name: /^Weekly explainer #14/ }).element().getBoundingClientRect();
  const actors = [...crew.element().querySelectorAll(".office-actor")].map((a) => a.getBoundingClientRect());
  expect(actors).toHaveLength(3);
  for (const a of actors) expect(a.top + a.height / 4).toBeGreaterThanOrEqual(board.bottom);
});

test("in the cartoon room at 1440 by 900 the crew is plain to see: each over half as tall as the whiteboard is wide", async () => {
  await page.viewport(1440, 900);
  try {
    const { screen } = await office([weekly], [agent("planner", "planner", "run_14", 6), agent("worker-1", "worker", "run_14", 6)]);
    const crew = crewOf(screen, "Weekly explainer #14");
    await expect.element(crew.getByRole("img", { name: "Worker 1, working" })).toBeVisible();
    const board = screen.getByRole("list", { name: "Pods" }).getByRole("button", { name: /^Weekly explainer #14/ }).element().getBoundingClientRect();
    for (const a of crew.element().querySelectorAll(".office-actor")) {
      expect(a.getBoundingClientRect().height).toBeGreaterThan(board.width / 2);
      expect(a.getBoundingClientRect().height).toBeLessThan(board.width);
    }
  } finally {
    await page.viewport(1280, 800);
  }
});

const more = (n: number) =>
  Array.from({ length: n }, (_, i) => run("running", stages(`run_m${i}`, 3, 1), { id: `run_m${i}`, name: `More #${i + 1}`, created_at: `2026-10-31T09:${10 + i}:00Z` }));
const crews = [
  agent("planner", "planner", "run_14", 6),
  agent("worker-1", "worker", "run_14", 6),
  agent("worker-2", "worker", "run_14", 6),
  agent("worker-1", "worker", "run_15", 2),
  agent("worker-2", "worker", "run_m0", 1),
];

test.each([
  [1280, 720],
  [390, 844],
  [375, 667],
])("at %i by %i six live runs each have a pod on screen, and neither the page nor the pods scroll", async (w, h) => {
  await page.viewport(w, h);
  try {
    const { screen } = await office([weekly, long, gated, paused, retro, ...more(1)], crews);
    const pods = screen.getByRole("list", { name: "Pods" });
    await expect.element(crewOf(screen, "More #1")).toBeVisible();
    expect(pods.getByRole("button").elements()).toHaveLength(6);
    const floor = pods.element();
    const box = floor.getBoundingClientRect();
    for (const pod of floor.children) {
      const r = pod.getBoundingClientRect();
      expect(r.top).toBeGreaterThanOrEqual(box.top);
      expect(r.bottom).toBeLessThanOrEqual(box.bottom + 1);
      expect(r.right).toBeLessThanOrEqual(box.right + 1);
    }
    expect(getComputedStyle(floor).overflowY).not.toMatch(/auto|scroll/);
    expect(document.documentElement.scrollHeight).toBeLessThanOrEqual(innerHeight);
  } finally {
    await page.viewport(1280, 800);
  }
});

test("live runs past what a phone's floor holds wait behind a chip that opens the quest board", async () => {
  await page.viewport(375, 667);
  try {
    const { screen } = await office([weekly, long, ...more(10)], crews);
    const pods = screen.getByRole("list", { name: "Pods" });
    const chip = pods.getByRole("button", { name: /^\+\d+ more$/ });
    await expect.element(chip).toBeVisible();
    const hidden = Number(/\d+/.exec(chip.element().textContent ?? "")![0]);
    expect(pods.getByRole("button", { name: /stage/ }).elements().length + hidden).toBe(12);
    const box = pods.element().getBoundingClientRect();
    for (const pod of pods.element().children) expect(pod.getBoundingClientRect().bottom).toBeLessThanOrEqual(box.bottom + 1);
    await chip.click();
    const board = screen.getByRole("complementary", { name: "Quest board" });
    await expect.element(board).toBeVisible();
    await expect.element(board.getByRole("button", { name: /^More #10/ })).toBeVisible();
  } finally {
    await page.viewport(1280, 800);
  }
});

test("the line at Your office keeps clear of the idle cast; each alert is over its asker's tag", async () => {
  const { screen } = await office([gated], [agent("judge", "judge", "run_19", 1, "done")], [gate("pick", { run_id: "run_19", run_name: "Pick a topic", stage_id: "run_19_s2", stage_index: 2 })]);
  const line = screen.getByRole("list", { name: "Line at Your office" });
  const asker = line.getByRole("button", { name: /^Rex asks/ });
  await expect.element(asker).toBeVisible();
  const tag = asker.element().querySelector(".office-tag")!.getBoundingClientRect();
  expect(asker.element().querySelector(".office-alert")!.getBoundingClientRect().bottom).toBeLessThanOrEqual(tag.top);
  const cast = screen.getByRole("list", { name: "The cast" }).element().getBoundingClientRect();
  expect(cast.right).toBeLessThanOrEqual(line.element().getBoundingClientRect().left);
  // Your office says how many wait on a line of its own, under its name.
  const mine = screen.getByRole("button", { name: "Your office, 1 waiting" }).element();
  expect(mine.querySelector("small")!.getBoundingClientRect().top).toBeGreaterThanOrEqual(mine.querySelector("b")!.getBoundingClientRect().bottom);
});
