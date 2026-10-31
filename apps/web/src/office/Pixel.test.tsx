import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

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

async function office(pack: string) {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(world(pack)),
    "GET /api/worlds/world_1/runs": () => Response.json([weekly]),
    "GET /api/runs/run_14/agents": () => Response.json([worker]),
    "GET /api/worlds/world_1/cast": () => Response.json([{ name: "worker-1", role: "worker", version: 1, prompt: "", tools: null, updated_at: null }]),
    "GET /api/sprite-packs": () => Response.json([toon, tiny]),
  });
  return mountAt("/worlds/world_1/office");
}

const board = (screen: Awaited<ReturnType<typeof office>>["screen"]) => 
  screen.getByRole("list", { name: "Pods" }).getByRole("button", { name: "Weekly explainer #14, stage 1 of 1, Render scenes" });

test("a pixel pack draws the pixel room, with Your office, in a pixel font", async () => {
  const { screen } = await office("tiny");
  await expect.element(board(screen)).toBeVisible();
  expect(getComputedStyle(board(screen).element()).fontFamily).toContain("Pixelify Sans");
  await expect.element(screen.getByText("Your office")).toBeVisible();
});

test("a cartoon pack keeps the cartoon room", async () => {
  const { screen } = await office("kenney-toon");
  await expect.element(board(screen)).toBeVisible();
  expect(getComputedStyle(board(screen).element()).fontFamily).not.toContain("Pixelify Sans");
  expect(screen.getByText("Your office").elements()).toHaveLength(0);
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
  expect(art).toEqual(["/api/sprite-packs/tiny/props/table.png", expect.stringMatching(/^data:image\/png/), "/api/sprite-packs/tiny/props/cup.png", expect.stringMatching(/^data:image\/png/)]);
});
