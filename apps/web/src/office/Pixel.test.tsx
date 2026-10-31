import { page } from "vitest/browser";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";
import { scaleFor } from "./pixel";

const recent = new Date(Date.now() - 60 * 60 * 1000).toISOString();
const world = (pack: string) => ({ id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00", run_cap: 2, skin: { view: "office", pack } });
const weekly = run("running", [stage(0, "Render scenes", "swarm", "running", { id: "run_14_s0" })], { id: "run_14", name: "Weekly explainer #14" });
const worker = {
  id: "agent_1", world_id: "world_1", run_id: "run_14", stage_id: "run_14_s0", name: "worker-1", role: "worker", status: "working",
  session_id: null, cost_usd: 0, created_at: recent, updated_at: recent,
};
const ana = { name: "Ana", poses: { idle: ["idle.png"], work: ["work.png"] } };
/** A tiny pixel pack: 16 by 16 frames, its own table and cup, every other prop left to the room. */
const tiny = {
  id: "tiny", name: "Tiny pixels", licence: "Test art", frame: [16, 16], characters: { ana }, casting: {},
  room: { style: "pixel", props: { table_on: "props/table.png", cup: "props/cup.png" }, poses_side: false },
};
const toon = { id: "kenney-toon", name: "Toon Characters", licence: "CC0", frame: [192, 256], characters: { ana }, casting: {}, room: { style: "cartoon", props: {}, poses_side: false } };

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function office(pack: string, { crew = [worker], packs = async () => Response.json([toon, tiny]), runs = [weekly], cap = 2 } = {}) {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ ...world(pack), run_cap: cap }),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "GET /api/runs/run_14/agents": () => Response.json(crew),
    "GET /api/worlds/world_1/cast": () =>
      Response.json(crew.map((a) => ({ name: a.name, role: "worker", version: 1, prompt: "", tools: null, updated_at: null }))),
    "GET /api/sprite-packs": packs,
  });
  return mountAt("/worlds/world_1/office");
}

const board = (screen: Awaited<ReturnType<typeof office>>["screen"]) => 
  screen.getByRole("list", { name: "Pods" }).getByRole("button", { name: "Weekly explainer #14, stage 1 of 1, Render scenes" });

test("a pixel pack draws the pixel room, with Your office, in a pixel font", async () => {
  const { screen } = await office("tiny");
  await expect.element(board(screen)).toBeVisible();
  expect(getComputedStyle(board(screen).element()).fontFamily).toContain("Pixelify Sans");
  // The font itself has loaded, not just been asked for.
  await vi.waitFor(() => expect([...document.fonts].some((f) => f.family.replace(/"/g, "") === "Pixelify Sans" && f.status === "loaded")).toBe(true));
  await expect.element(screen.getByRole("button", { name: "Your office, 0 waiting" })).toBeVisible();
});

test("a cartoon pack keeps the cartoon room", async () => {
  const { screen } = await office("kenney-toon");
  await expect.element(board(screen)).toBeVisible();
  expect(getComputedStyle(board(screen).element()).fontFamily).not.toContain("Pixelify Sans");
  expect(screen.getByRole("region", { name: "The office" }).element().querySelector("canvas")).toBeNull();
});

test("the room waits for the pack list instead of drawing the cartoon room first", async () => {
  let answer = (_: Response) => {};
  const { screen } = await office("tiny", { packs: () => new Promise<Response>((r) => (answer = r)) });
  const room = screen.getByRole("region", { name: "The office" });
  await expect.element(room).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("list", { name: "Pods" }).elements()).toHaveLength(0);
  answer(Response.json([toon, tiny]));
  await expect.element(board(screen)).toBeVisible();
  expect(getComputedStyle(board(screen).element()).fontFamily).toContain("Pixelify Sans");
});

/** How many screen pixels one of the element's own pixels takes. */
const scaleOf = (el: Element) => el.getBoundingClientRect().width / (el as HTMLElement).offsetWidth;

test("characters, props and text share one whole-number pixel scale", async () => {
  const { screen } = await office("tiny");
  const actor = screen.getByRole("img", { name: "Worker 1, working" });
  await expect.element(actor).toBeVisible();
  const room = screen.getByRole("region", { name: "The office" }).element();
  // The free pod's table is the room's own, drawn at its art size.
  const free = room.querySelector<HTMLImageElement>(".office-spare img[src^='data:']");
  await vi.waitFor(() => expect(free?.complete && free.naturalWidth).toBeTruthy());

  const k = scaleOf(actor.element());
  expect(Number.isInteger(k) && k >= 1).toBe(true);
  expect((actor.element() as HTMLElement).offsetWidth).toBe(16);
  expect(free?.offsetWidth).toBe(free?.naturalWidth);
  expect(scaleOf(free as Element)).toBe(k);
  expect(scaleOf(board(screen).element())).toBe(k);
});

test("the pack's own props replace the room's; the ones it leaves out are drawn by the room", async () => {
  const { screen } = await office("tiny");
  const pod = screen.getByRole("list", { name: "Pods" }).getByRole("listitem").filter({ hasText: "Weekly explainer #14" });
  await expect.element(pod).toBeVisible();
  const art = [...pod.element().querySelectorAll("img:not([alt]), img[alt='']")].map((i) => i.getAttribute("src"));
  const drawn = expect.stringMatching(/^data:image\/png/);
  expect(art).toEqual(["/api/sprite-packs/tiny/props/table.png", drawn, "/api/sprite-packs/tiny/props/cup.png", drawn]);
});

test("each agent at a pod has a laptop in front of them", async () => {
  const three = [1, 2, 3].map((n) => ({ ...worker, id: `agent_${n}`, name: `worker-${n}` }));
  const { screen } = await office("tiny", { crew: three });
  const pod = screen.getByRole("list", { name: "Pods" }).getByRole("listitem").filter({ hasText: "Weekly explainer #14" });
  await expect.element(pod.getByRole("img", { name: "Worker 3, working" })).toBeVisible();
  expect(pod.element().querySelectorAll(".office-prop-laptop_on")).toHaveLength(3);
});

test("a table has at least two laptops, standing on its top and clear of the whiteboard", async () => {
  // The free pod: the room draws its table and laptops, so they have their art sizes.
  const { screen } = await office("tiny");
  await expect.element(board(screen)).toBeVisible();
  const pod = document.querySelector(".office-spare")!;
  const art = [...pod.querySelectorAll<HTMLImageElement>("img[src^='data:']")];
  await vi.waitFor(() => expect(art.every((i) => i.complete && i.naturalWidth)).toBe(true));
  const table = pod.querySelector(".office-table")!.getBoundingClientRect();
  const wb = pod.querySelector(".office-board")!.getBoundingClientRect();
  const laptops = [...pod.querySelectorAll(".office-prop-laptop_off")].map((l) => l.getBoundingClientRect());
  expect(laptops).toHaveLength(2);
  for (const l of laptops) {
    expect(l.bottom).toBeGreaterThan(table.top);
    expect(l.bottom).toBeLessThan(table.bottom);
    expect(l.top).toBeGreaterThanOrEqual(wb.bottom);
  }
});

test("the room's scale is whole device pixels: 2x on a 1280 by 720 screen, 4/3 on a 3x phone, 1.5 on a 1x one", async () => {
  expect(scaleFor(985, 630, 1)).toBe(2);
  expect(scaleFor(808, 674, 1)).toBe(1);
  expect(scaleFor(985, 630, 2)).toBe(2);
  await page.viewport(390, 844);
  expect(scaleFor(374, 560, 3)).toBe(4 / 3);
  expect(scaleFor(359, 383, 1)).toBe(1.5);
  await page.viewport(1280, 800);
});

test("on a mid-size screen the room keeps the art size it is laid out for, nothing overlapping", async () => {
  // A room of about 808 by 674: the bigger pixels would leave only 404 by 337 art pixels.
  await page.viewport(1100, 760);
  const { screen } = await office("tiny");
  const mine = screen.getByRole("button", { name: "Your office, 0 waiting" });
  await expect.element(mine).toBeVisible();
  const room = screen.getByRole("region", { name: "The office" }).element().getBoundingClientRect();
  const k = scaleOf(screen.getByRole("img", { name: "Worker 1, working" }).element());
  expect(room.width / k).toBeGreaterThanOrEqual(430);
  expect(room.height / k).toBeGreaterThanOrEqual(280);
  const apart = (a: DOMRect, b: DOMRect) => a.left >= b.right || b.left >= a.right || a.top >= b.bottom || b.top >= a.bottom;
  const yours = mine.element().getBoundingClientRect();
  expect(apart(yours, screen.getByRole("group", { name: "Lobby sofa" }).element().getBoundingClientRect())).toBe(true);
  expect(apart(yours, screen.getByRole("list", { name: "Pods" }).element().getBoundingClientRect())).toBe(true);
});

test("on a 375 by 667 phone six live runs each have a pod in the pixel room, at a smaller scale, and nothing scrolls", async () => {
  await page.viewport(375, 667);
  try {
    const six = [weekly, ...[1, 2, 3, 4, 5].map((n) => run("running", [stage(0, "Draft", "swarm", "running", { id: `run_${n}_s0` })], { id: `run_${n}`, name: `Run ${n}` }))];
    const { screen } = await office("tiny", { runs: six, cap: 8 });
    const pods = screen.getByRole("list", { name: "Pods" });
    await expect.element(screen.getByRole("img", { name: "Worker 1, working" })).toBeVisible();
    expect(pods.getByRole("button").elements()).toHaveLength(6);
    const box = pods.element().getBoundingClientRect();
    for (const pod of pods.element().children) {
      const r = pod.getBoundingClientRect();
      expect(r.top).toBeGreaterThanOrEqual(box.top);
      expect(r.bottom).toBeLessThanOrEqual(box.bottom + 1);
    }
    expect(document.documentElement.scrollHeight).toBeLessThanOrEqual(innerHeight);
  } finally {
    await page.viewport(1280, 800);
  }
});
