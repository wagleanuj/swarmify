import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = {
  id: "world_1",
  name: "Video",
  purpose: "Weekly shorts.",
  created_at: "2026-10-26T10:00:00+00:00",
  run_cap: 3,
  skin: { view: "office" },
};
/** A world dressed in Crew: a pack of its own and an agent's own look. */
const dressed = { ...video, skin: { view: "office", pack: "mine", cast: { judge: { name: "Rex" } } } };
const spend = { month: "2026-10", spent_usd: 3.5, monthly_budget_usd: 20 };

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  localStorage.removeItem("swarmify.skin");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Answers the world, its spend and runs, and records each settings write. */
function office(world: object = video, runs = [run("running", [])]) {
  const saved: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/spend": () => Response.json(spend),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "PATCH /api/worlds/world_1": async (req) => {
      const body = (await req.json()) as { skin: { view: string } };
      saved.push(body);
      return Response.json({ ...world, ...body });
    },
  });
  return { saved, calls };
}

test("the office has its own shell: a top bar of figures beside their reference, no sidebar", async () => {
  office();
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await expect.element(screen.getByTestId("office-spend")).toHaveTextContent("$3.50 of $20.00 this month");
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("1 of 3 runs going");
  await expect.element(screen.getByRole("region", { name: "The office" })).toBeVisible();
  expect(screen.getByRole("navigation", { name: "Main" }).elements()).toHaveLength(0);
  expect(screen.getByRole("navigation", { name: "Tabs" }).elements()).toHaveLength(0);

  // A run that starts holds a slot; a child run spends its parent's.
  FakeEventSource.emit("run.updated", { payload: run("waiting_gate", [], { id: "run_2" }) });
  FakeEventSource.emit("run.updated", { payload: run("running", [], { id: "run_3", parent_run_id: "run_1" }) });
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("2 of 3 runs going");

  // The run cap and name follow a change made in Settings elsewhere.
  FakeEventSource.emit("world.updated", { payload: { id: "world_1", run_cap: 5, name: "Shorts" } });
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("2 of 5 runs going");
  await expect.element(screen.getByRole("heading", { name: "Shorts", level: 1 })).toBeVisible();
});

test("a world with no budget says so instead of a bare figure", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/spend": () => Response.json({ ...spend, spent_usd: 0, monthly_budget_usd: null }),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByTestId("office-spend")).toHaveTextContent("$0.00 spent, no monthly budget");
  await expect.element(screen.getByTestId("office-runs")).toHaveTextContent("0 of 3 runs going");
});

test("Plain view saves the setting and opens the plain overview in the sidebar shell", async () => {
  const { saved } = office(dressed);
  const { screen, router } = await mountAt("/worlds/world_1/office");
  await screen.getByRole("button", { name: "Plain view" }).click();
  await expect.element(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
  // The rest of the skin goes back as it was, so the cast keeps its looks.
  expect(saved).toEqual([{ skin: { view: "plain", pack: "mine", cast: { judge: { name: "Rex" } } } }]);
  expect(router.state.location.pathname).toBe("/worlds/world_1");
  await expect.element(screen.getByRole("button", { name: "Office" })).toBeVisible();
});

test("Office on the plain overview saves the setting and opens the office", async () => {
  const { saved } = office({ ...dressed, skin: { ...dressed.skin, view: "plain" } });
  const { screen, router } = await mountAt("/worlds/world_1");
  // Crew dresses the cast in another tab; the switch sends that, not the skin the page loaded.
  FakeEventSource.emit("world.updated", { payload: { id: "world_1", skin: { view: "plain", pack: "mine", cast: { judge: { name: "Erin" } } } } });
  await screen.getByRole("button", { name: "Office" }).click();
  await expect.element(screen.getByRole("button", { name: "Plain view" })).toBeVisible();
  expect(saved).toEqual([{ skin: { view: "office", pack: "mine", cast: { judge: { name: "Erin" } } } }]);
  expect(router.state.location.pathname).toBe("/worlds/world_1/office");
  expect(screen.getByRole("navigation", { name: "Main" }).elements()).toHaveLength(0);
});

test("a refused switch stays put and says so", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "PATCH /api/worlds/world_1": () => Response.json({ detail: "no" }, { status: 422 }),
  });
  const { screen, router } = await mountAt("/worlds/world_1/office");
  await screen.getByRole("button", { name: "Plain view" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Could not switch to the plain view. Try again.");
  expect(router.state.location.pathname).toBe("/worlds/world_1/office");
});

test("the world links open each world in its chosen view", async () => {
  const plain = { ...video, id: "world_2", name: "Research", skin: { view: "plain" } };
  fakeFetch({ "GET /api/worlds": () => Response.json([video, plain]) });
  const { screen } = await mountAt("/");
  const worlds = screen.getByRole("region", { name: "Worlds" });
  await expect.element(worlds.getByRole("link", { name: "Video" })).toHaveAttribute("href", "/worlds/world_1/office");
  await expect.element(worlds.getByRole("link", { name: "Research" })).toHaveAttribute("href", "/worlds/world_2");
  // The sidebar's world link follows the current world's view too.
  await expect
    .element(screen.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Overview" }))
    .toHaveAttribute("href", "/worlds/world_1/office");
});

test("an unknown world says so in the office", async () => {
  fakeFetch({});
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByRole("alert")).toHaveTextContent("No such world. Back to your worlds.");
});

const member = (name: string, role: string) => ({ name, role, version: 1, prompt: "", tools: null, updated_at: null });
const idle = (name: string) => ({ name, poses: { idle: ["idle.png"], work: ["work0.png"] } });
/** The bundled pack as `GET /api/sprite-packs` lists it, cut down, beside an installed one. */
const packs = [
  {
    id: "kenney-toon",
    name: "Toon Characters",
    licence: "CC0",
    characters: { ada: idle("Ada"), bo: idle("Bo"), blank: { name: "Blank", poses: { work: ["work0.png"] } } },
    casting: {
      planner: { character: "ada", name: "Mira" },
      judge: { character: "gone", name: "Rex" },
      tester: { character: "bo", name: "Tess" },
    },
    problems: ["blank has no idle pose"],
  },
  { id: "mine", name: "Mine", licence: "", characters: { zed: idle("Zed") }, casting: { planner: { character: "zed" } } },
];

test("the cast stands in the office in their pack characters", async () => {
  let cast = [member("planner", "planner"), member("worker-1", "worker"), member("judge", "judge"), member("qa", "tester")];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ ...video, agent_count: 1 }),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/sprite-packs": () => Response.json(packs),
  });
  const { screen } = await mountAt("/worlds/world_1/office");
  const standing = screen.getByRole("list", { name: "The cast" });
  // The pack's casting by agent name; no casting gets a spare character in turn with the plain
  // name; a cast character the pack lacks falls back to a spare one, keeping the cast name.
  await expect.element(standing.getByRole("img", { name: "Mira, Planner" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/idle.png");
  await expect.element(standing.getByRole("img", { name: "Worker 1" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/idle.png");
  await expect.element(standing.getByRole("img", { name: "Rex, Judge" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/idle.png");
  // The pack's casting by role when none names the agent.
  await expect.element(standing.getByRole("img", { name: "Tess, qa" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/idle.png");
  // The retro agent is in the skin's cast too, in the next spare character.
  await expect.element(standing.getByRole("img", { name: "Retro" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/bo/idle.png");
  expect(standing.getByRole("img").elements()).toHaveLength(5);

  // A worker added in Settings joins the cast without a reload.
  cast = [member("planner", "planner"), member("worker-1", "worker"), member("worker-2", "worker"), member("judge", "judge"), member("qa", "tester")];
  FakeEventSource.emit("world.updated", { payload: { id: "world_1", agent_count: 2 } });
  await expect.element(standing.getByRole("img", { name: "Worker 2" })).toBeVisible();
});

test("the office ignores the plain pages' Pied Piper names", async () => {
  localStorage.setItem("swarmify.skin", "pied-piper");
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json([member("worker-1", "worker")]),
    "GET /api/sprite-packs": () => Response.json(packs),
  });
  const { screen } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByRole("img", { name: "Worker 1" })).toBeVisible();
});
