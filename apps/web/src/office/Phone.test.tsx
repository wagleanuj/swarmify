import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";
import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const world = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00", run_cap: 6, agent_count: 1, skin: { view: "office" } };
const poses = (c: string) => ({ name: c, poses: { idle: ["idle.png"], talk: ["talk.png"] } });
const pack = {
  id: "kenney-toon",
  name: "Toon Characters",
  licence: "CC0",
  characters: { ada: poses("Ada"), bo: poses("Bo") },
  casting: { planner: { character: "ada", name: "Mira" }, judge: { character: "bo", name: "Rex" } },
  problems: [],
};
const member = (name: string, role: string) => ({ name, role, version: 1, prompt: "", tools: null, updated_at: null });
const cast = [member("planner", "planner"), member("worker-1", "worker"), member("judge", "judge")];

const stages = (id: string) =>
  Array.from({ length: 6 }, (_, i) => stage(i, i === 2 ? "Review" : `Step ${i + 1}`, i === 2 ? "gate" : "swarm", i < 2 ? "done" : i === 2 ? "waiting" : "pending", { id: `${id}_s${i}` }));
const runs = [
  run("waiting_gate", stages("run_a"), { id: "run_a", name: "Launch thread" }),
  run("running", [stage(0, "Draft", "swarm", "running", { id: "run_b_s0" })], { id: "run_b", name: "Weekly explainer" }),
];
const open = () => [
  gate("approve", { run_id: "run_a", run_name: "Launch thread", stage_id: "run_a_s2", stage_index: 2, ask: "Ship it to Publish?", earlier_stages: ["research", "draft", "edit"] }),
  gate("confirm", { run_id: "run_b", run_name: "Weekly explainer", stage_id: "run_b_s1", ask: "Post it?", opened_at: "2026-10-31T09:00:00Z" }),
];

/** One world with two runs, each waiting on the operator; records every request. */
function office() {
  const gates = open();
  const saved: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/worlds/world_1": () => Response.json(world),
    "GET /api/worlds/world_1/spend": () => Response.json({ month: "2026-10", spent_usd: 3.5, monthly_budget_usd: 20 }),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "GET /api/worlds/world_1/cast": () => Response.json(cast),
    "GET /api/sprite-packs": () => Response.json([pack]),
    "GET /api/gates": () => Response.json(gates),
    "GET /api/runs/run_a/agents": () => Response.json([]),
    "GET /api/runs/run_b/agents": () => Response.json([]),
    "GET /api/runs/run_a/artifacts": () => Response.json([]),
    "PATCH /api/worlds/world_1": async (req) => {
      saved.push(await req.json());
      return Response.json(world);
    },
  });
  return { calls, saved };
}

beforeEach(() => FakeEventSource.install());
afterEach(async () => {
  localStorage.removeItem("swarmify.office.night");
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  await page.viewport(1280, 800);
});

/** The page itself never scrolls, and the box sits wholly on screen. */
function fits(box: Element) {
  const root = document.documentElement;
  expect(root.scrollHeight).toBeLessThanOrEqual(innerHeight);
  expect(root.scrollWidth).toBeLessThanOrEqual(innerWidth);
  const r = box.getBoundingClientRect();
  expect(r.top).toBeGreaterThanOrEqual(0);
  expect(r.bottom).toBeLessThanOrEqual(innerHeight);
}

const SIZES: [number, number][] = [
  [375, 667],
  [390, 844],
  [1280, 720],
];

test.each(SIZES)("at %i by %i no office screen scrolls the page", async (w, h) => {
  await page.viewport(w, h);
  office();
  const { screen, router } = await mountAt("/worlds/world_1/office");
  await expect.element(screen.getByRole("region", { name: "The office" })).toBeVisible();
  fits(screen.getByRole("region", { name: "The office" }).element());

  await router.navigate({ to: "/worlds/$worldId/office/talk", params: { worldId: "world_1" } });
  const choices = screen.getByRole("list", { name: "Choices" });
  await expect.element(choices.getByRole("button", { name: "1 Approve" })).toBeVisible();
  for (const b of choices.getByRole("button").elements()) fits(b);

  await router.navigate({ to: "/worlds/$worldId/office/crew", params: { worldId: "world_1" }, search: { agent: "judge" } });
  await expect.element(screen.getByRole("region", { name: "Stage" })).toBeVisible();
  fits(screen.getByRole("region", { name: "Stage" }).element());
});

test("on a phone the office has a bottom tab bar, a banner of who needs you, and the quest board as a sheet", async () => {
  await page.viewport(390, 844);
  office();
  const { screen, router } = await mountAt("/worlds/world_1/office");
  const tabs = screen.getByRole("navigation", { name: "Office tabs" });
  await expect.element(tabs.getByRole("link", { name: "Needs you 2 waiting" })).toBeVisible();
  expect(tabs.getByRole("link").elements().map((a) => a.textContent)).toEqual(["Office", "Needs you 2", "Crew"]);
  await expect.element(tabs.getByRole("link", { name: "Office" })).toHaveAttribute("aria-current", "page");
  await expect.element(tabs.getByRole("button", { name: "Night" })).toBeVisible();
  expect(tabs.element().getBoundingClientRect().bottom).toBeLessThanOrEqual(844);

  // The oldest decision is pinned over the office.
  const banner = screen.getByRole("button", { name: /is waiting on you/ });
  await expect.element(banner).toHaveTextContent("Mira is waiting on you: Ship it to Publish? and 1 more");

  // The quest board waits in a sheet.
  const board = screen.getByRole("complementary", { name: "Quest board" });
  await expect.element(board).not.toBeInTheDocument();
  await screen.getByRole("button", { name: "Quest board" }).click();
  await expect.element(board.getByRole("button", { name: /^Weekly explainer/ })).toBeVisible();
  fits(board.element());
  await screen.getByRole("button", { name: "Done" }).click();
  await expect.element(board).not.toBeInTheDocument();

  await banner.click();
  expect(router.state.location.pathname).toBe("/worlds/world_1/office/talk");
  expect(router.state.location.search).toEqual({ gate: "run_a_s2" });
  await expect.element(screen.getByRole("heading", { name: "Mira Planner", level: 1 })).toBeVisible();
  await tabs.getByRole("link", { name: "Crew" }).click();
  await expect.element(screen.getByRole("region", { name: "Stage" })).toBeVisible();
});

test("on a short phone Talk drops the large character so every choice fits", async () => {
  await page.viewport(375, 667);
  office();
  const { screen } = await mountAt("/worlds/world_1/office/talk?gate=run_a_s2");
  const choices = screen.getByRole("list", { name: "Choices" });
  await expect.element(choices.getByRole("button", { name: "2 Send back to edit" })).toBeVisible();
  await expect.element(screen.getByRole("img", { name: /asking/ })).not.toBeInTheDocument();
  expect(choices.getByRole("button").elements()).toHaveLength(4);
  for (const b of choices.getByRole("button").elements()) fits(b);
});

test("on a phone the Crew form is a sheet over the stage", async () => {
  await page.viewport(375, 667);
  const { saved } = office();
  const { screen } = await mountAt("/worlds/world_1/office/crew?agent=judge");
  const form = screen.getByRole("form", { name: "Dress Judge" });
  await expect.element(screen.getByRole("region", { name: "Stage" })).toBeVisible();
  await expect.element(form).not.toBeInTheDocument();
  await screen.getByRole("button", { name: "Dress Rex" }).click();
  await expect.element(form).toBeVisible();
  fits(form.element());
  await form.getByRole("textbox", { name: /^Name/ }).fill("Judy");
  await form.getByRole("button", { name: "Save Judy" }).click();
  await expect.poll(() => saved).toHaveLength(1);
  await form.getByRole("button", { name: "Done" }).click();
  await expect.element(form).not.toBeInTheDocument();
});

test("day and night and full screen are this browser's, never the world's", async () => {
  const { calls } = office();
  const full = vi.spyOn(document.documentElement, "requestFullscreen").mockResolvedValue();
  const { screen, router } = await mountAt("/worlds/world_1/office");
  const night = screen.getByRole("button", { name: "Night" });
  await expect.element(night).toHaveAttribute("aria-pressed", "false");
  await night.click();
  await expect.element(night).toHaveAttribute("aria-pressed", "true");
  await screen.getByRole("button", { name: "Full screen" }).click();
  expect(full).toHaveBeenCalledOnce();

  // Every office screen keeps it, and the world was never written.
  await router.navigate({ to: "/worlds/$worldId/office/crew", params: { worldId: "world_1" } });
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Night" })).toHaveAttribute("aria-pressed", "true");
  expect(calls.filter((c) => !c.startsWith("GET"))).toEqual([]);

  await screen.getByRole("button", { name: "Night" }).click();
  await expect.element(screen.getByRole("button", { name: "Night" })).toHaveAttribute("aria-pressed", "false");
});
