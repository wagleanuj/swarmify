import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { Agent } from "../api/events";
import type { Run } from "../api/runs";
import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
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

/** The office over `runs`, with the agents each run's agents read answers. */
function office(runs: Run[], agents: Agent[]) {
  const routes: Record<string, () => Response> = {
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
  );
  const weeklyCrew = crewOf(screen, "Weekly explainer #14");
  await expect.element(weeklyCrew.getByRole("img", { name: "Mira, Planner, thinking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/think.png");
  await expect.element(weeklyCrew.getByRole("img", { name: "Worker 1, working" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/work0.png");
  await expect.element(weeklyCrew.getByRole("img", { name: "Worker 2, stuck" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/stuck.png");
  expect(weeklyCrew.getByRole("img").elements()).toHaveLength(3);
  await expect.element(crewOf(screen, "Long haul").getByRole("img", { name: "Worker 1, working" })).toBeVisible();

  const asking = crewOf(screen, "Pick a topic");
  await expect.element(asking.getByRole("img", { name: "Rex, Judge, asking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/talk.png");
  expect(asking.getByRole("img").elements()).toHaveLength(1);
  await expect.element(asking.getByText("!")).toBeVisible();
  await expect.element(crewOf(screen, "On hold").getByRole("img", { name: "Worker 1, stuck" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/stuck.png");
  await expect
    .element(crewOf(screen, "Wrap up").getByRole("img", { name: "Worker 2, at the whiteboard" }))
    .toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/back.png");

  // Off the floor: shipped after a run that is done, stuck after one that failed, else idle.
  const bench = screen.getByRole("list", { name: "The cast" });
  await expect.element(bench.getByRole("img", { name: "Tess, qa, shipped" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/cheer.png");
  await expect.element(bench.getByRole("img", { name: "Budget, stuck" })).toBeVisible();
  expect(bench.getByRole("img").elements().map((i) => i.getAttribute("alt"))).toEqual(["Tess, qa, shipped", "Budget, stuck"]);
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
  await expect.element(pods.getByRole("listitem").filter({ hasText: "Long haul" }).getByText("script.md")).toBeVisible();
  expect(pods.getByRole("listitem").filter({ hasText: "Weekly explainer #14" }).getByText("script.md").elements()).toHaveLength(0);
  await expect.element(screen.getByRole("region", { name: "Radio" })).toMatchTextContent(/\d\d:\d\d Worker 1 made script\.md/);
});

test("an agent.updated event moves the agent, and the radio says so in the cast's names", async () => {
  const worker = agent("worker-1", "worker", "run_14", 6);
  const planner = agent("planner", "planner", "run_14", 6);
  const { screen } = await office([weekly], [worker, planner]);
  const radio = screen.getByRole("region", { name: "Radio" });
  await expect.element(radio).toMatchTextContent(/Quiet for now\./);
  const pod = crewOf(screen, "Weekly explainer #14");
  await expect.element(pod.getByRole("img", { name: "Worker 1, working" })).toBeVisible();

  // Done with the stage, the worker leaves the desks for the rest of the cast.
  FakeEventSource.emit("agent.updated", { payload: { ...worker, status: "done" } });
  await expect.element(screen.getByRole("list", { name: "The cast" }).getByRole("img", { name: "Worker 1" })).toBeVisible();
  expect(pod.getByRole("img", { name: "Worker 1, working" }).elements()).toHaveLength(0);
  await expect.element(radio).toMatchTextContent(/Worker 1 finished Render scenes/);

  // The run moves on to a gate: the desks empty and the planner stands up to ask.
  FakeEventSource.emit("agent.updated", { payload: { ...planner, status: "done" } });
  FakeEventSource.emit("run.updated", { payload: { ...weekly, status: "waiting_gate", stages: stages("run_14", 12, 7, "gate", "waiting") } });
  await expect.element(pod.getByRole("img", { name: "Mira, Planner, asking" })).toBeVisible();
  expect(pod.getByRole("img").elements()).toHaveLength(1);
  expect(radio.getByRole("listitem").elements().map((li) => li.textContent?.slice(6))).toEqual(["Mira finished Render scenes", "Worker 1 finished Render scenes"]);
});

test("with reduced motion every character holds its pose; with motion an arriving agent walks in", async () => {
  const { screen } = await office([weekly], [agent("worker-1", "worker", "run_14", 6)]);
  const pod = crewOf(screen, "Weekly explainer #14");
  const worker = pod.getByRole("img", { name: "Worker 1, working" });
  await expect.element(worker).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/work0.png");
  await new Promise((done) => setTimeout(done, 400));
  await expect.element(worker).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/work0.png");

  vi.spyOn(window, "matchMedia").mockReturnValue({ matches: false } as MediaQueryList);
  FakeEventSource.emit("agent.updated", { payload: agent("worker-2", "worker", "run_14", 6) });
  await expect.element(pod.getByRole("img", { name: "Worker 2, walking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/walk0.png");
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
  const { screen } = await office([gated], []);
  const asking = crewOf(screen, "Pick a topic");
  await expect.element(asking.getByRole("img", { name: "Mira, Planner, asking" })).toBeVisible();
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
