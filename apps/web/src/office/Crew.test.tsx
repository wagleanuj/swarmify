import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const plain = { view: "office", pack: "kenney-toon", cast: {} };
const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00", run_cap: 3, agent_count: 1, skin: plain };
const member = (name: string, role: string) => ({ name, role, version: 1, prompt: "", tools: null, updated_at: null });
const cast = [member("planner", "planner"), member("worker-1", "worker"), member("judge", "judge")];
const character = (name: string) => ({ name, poses: { idle: ["idle.png"], work: ["work0.png", "work1.png"] } });
/** The bundled pack, cut down, beside one the operator installed with its own casting. */
const packs = [
  {
    id: "kenney-toon",
    name: "Toon Characters",
    licence: "CC0, free for any use",
    author: "Kenney",
    characters: { ada: character("Ada"), bo: character("Bo") },
    casting: { planner: { character: "ada", name: "Mira", colour: "#ffc93c", catchphrase: "I have a plan." }, judge: { character: "bo", name: "Rex" } },
    problems: [],
  },
  {
    id: "mine",
    name: "My pack",
    licence: "Mine only, do not share",
    characters: { zed: character("Zed"), yan: character("Yan") },
    casting: { planner: { character: "zed", name: "Zed" }, "worker-1": { character: "yan", name: "Yan" }, judge: { character: "yan", name: "Judy" } },
    problems: [],
  },
];

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** Answers the world, its cast and the packs, and records each settings write. */
function crew(skin: object = plain) {
  let world = { ...video, skin };
  const saved: { skin: { pack: string; cast: Record<string, object> } }[] = [];
  fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/sprite-packs": () => Response.json(packs),
    "PATCH /api/worlds/world_1": async (req) => {
      const body = (await req.json()) as (typeof saved)[number];
      saved.push(body);
      const looks = Object.fromEntries(Object.entries(body.skin.cast).map(([n, c]) => [n, { character: null, name: null, colour: null, catchphrase: null, ...c }]));
      world = { ...world, skin: { ...body.skin, cast: looks } };
      return Response.json(world);
    },
  });
  return saved;
}

test("Crew lists the cast with face, name and role, and dresses the agent in one settings write", async () => {
  const saved = crew();
  const { screen } = await mountAt("/worlds/world_1/office/crew?agent=worker-1");
  const roster = screen.getByRole("complementary", { name: "Crew" });
  await expect.element(roster.getByRole("button", { name: "Mira Planner" })).toBeVisible();
  await expect.element(roster.getByRole("button", { name: "Worker 1 Worker 1" })).toHaveAttribute("aria-pressed", "true");
  await expect.element(roster.getByRole("button", { name: "Rex Judge" })).toBeVisible();
  expect(screen.getByRole("navigation", { name: "Main" }).elements()).toHaveLength(0);

  const form = screen.getByRole("form", { name: "Dress Worker 1" });
  await expect.element(form.getByText("Licence: CC0, free for any use · by Kenney")).toBeVisible();
  await form.getByRole("button", { name: "Ada" }).click();
  await form.getByLabelText("Name, up to 24 characters").fill("Jun");
  await form.getByLabelText("Colour").fill("#7fd6ff");
  await form.getByLabelText("Catchphrase, said when a run ships, up to 80 characters").fill("Rendered it twice.");
  // The stage shows the draft before it is saved.
  await expect.element(screen.getByRole("heading", { name: "Jun", level: 2 }).first()).toBeVisible();
  await form.getByRole("button", { name: "Save Jun" }).click();

  await expect.element(screen.getByRole("status")).toHaveTextContent("Saved. The office shows it now.");
  expect(saved).toEqual([
    { skin: { ...plain, cast: { "worker-1": { character: "ada", name: "Jun", colour: "#7fd6ff", catchphrase: "Rendered it twice." } } } },
  ]);
  await expect.element(roster.getByRole("button", { name: "Jun Worker 1" })).toBeVisible();
  await expect.element(screen.getByText("“Rendered it twice.”")).toBeVisible();
});

test("every pose previews, a pose the character lacks showing idle", async () => {
  crew();
  const { screen } = await mountAt("/worlds/world_1/office/crew?agent=planner");
  const stage = screen.getByRole("region", { name: "Stage" });
  await expect.element(stage.getByText("“I have a plan.”")).toBeVisible();
  const poses = stage.getByRole("group", { name: "Poses" });
  for (const pose of ["Idle", "Thinking", "Working", "Walking", "Asking", "Shipped", "Stuck", "Carrying", "At the whiteboard"]) {
    await expect.element(poses.getByRole("button", { name: pose })).toBeVisible();
  }
  await poses.getByRole("button", { name: "Working" }).click();
  await expect.element(stage.getByRole("img", { name: "Mira, Working" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/work0.png");
  await poses.getByRole("button", { name: "Asking" }).click();
  await expect.element(stage.getByRole("img", { name: "Mira, Asking" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/idle.png");
});

test("choosing a pack applies its casting, a per-agent change wins and reset restores", async () => {
  const saved = crew({ ...plain, cast: { judge: { character: null, name: "Erin", colour: null, catchphrase: null } } });
  const { screen } = await mountAt("/worlds/world_1/office/crew?agent=judge");
  const roster = screen.getByRole("complementary", { name: "Crew" });
  await expect.element(roster.getByRole("button", { name: "Erin Judge" })).toBeVisible();
  // A draft not yet saved goes with the pack switch instead of being thrown away.
  await screen.getByLabelText("Catchphrase, said when a run ships, up to 80 characters").fill("Order.");

  await screen.getByRole("group", { name: "Sprite pack" }).getByRole("button", { name: "My pack" }).click();
  // The pack dresses the cast; the operator's own name for the judge stays.
  await expect.element(roster.getByRole("button", { name: "Zed Planner" })).toBeVisible();
  await expect.element(roster.getByRole("button", { name: "Yan Worker 1" })).toBeVisible();
  await expect.element(roster.getByRole("button", { name: "Erin Judge" })).toBeVisible();
  await expect.element(screen.getByText("Licence: Mine only, do not share")).toBeVisible();
  expect(saved.at(-1)).toEqual({ skin: { ...plain, pack: "mine", cast: { judge: { name: "Erin", catchphrase: "Order." } } } });

  await screen.getByRole("button", { name: "Reset to the pack's casting" }).click();
  await expect.element(roster.getByRole("button", { name: "Judy Judge" })).toBeVisible();
  expect(saved.at(-1)).toEqual({ skin: { ...plain, pack: "mine", cast: {} } });
});

test("a character missing from the chosen pack is reported", async () => {
  crew({ ...plain, cast: { "worker-1": { character: "robot", name: null, colour: null, catchphrase: null } } });
  const { screen } = await mountAt("/worlds/world_1/office/crew?agent=worker-1");
  await expect
    .element(screen.getByRole("status"))
    .toHaveTextContent("Toon Characters has no character robot, so Worker 1 wears Bo. Pick another.");
});

test("a save the API refuses says so and keeps the form", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/sprite-packs": () => Response.json(packs),
    "PATCH /api/worlds/world_1": () => Response.json({ detail: "too long" }, { status: 422 }),
  });
  const { screen } = await mountAt("/worlds/world_1/office/crew?agent=judge");
  const form = screen.getByRole("form", { name: "Dress Judge" });
  await form.getByLabelText("Name, up to 24 characters").fill("Rexy");
  await form.getByRole("button", { name: "Save Rexy" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Could not save. Check the name is at most 24 characters and the catchphrase 80.");
  await expect.element(form.getByLabelText("Name, up to 24 characters")).toHaveValue("Rexy");
});

test("the office opens Crew from its bar and from an agent, and draws the world's casting", async () => {
  crew({ ...plain, cast: { "worker-1": { character: "ada", name: "Jun", colour: null, catchphrase: null } } });
  const { screen, router } = await mountAt("/worlds/world_1/office");
  const standing = screen.getByRole("list", { name: "The cast" });
  await expect.element(standing.getByRole("img", { name: "Jun, Worker 1" })).toHaveAttribute("src", "/api/sprite-packs/kenney-toon/ada/idle.png");
  await standing.getByRole("link", { name: "Jun, Worker 1" }).click();
  await expect.element(screen.getByRole("form", { name: "Dress Worker 1" })).toBeVisible();
  expect(router.state.location.search).toEqual({ agent: "worker-1" });
  await screen.getByRole("link", { name: "Back to the office" }).click();
  await screen.getByRole("link", { name: "Crew" }).click();
  await expect.element(screen.getByRole("form", { name: "Dress Planner" })).toBeVisible();
});
