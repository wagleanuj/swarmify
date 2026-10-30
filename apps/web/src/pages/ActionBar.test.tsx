import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import type { ArtifactFile } from "../api/events";
import { FakeEventSource, fakeFetch, gate, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

// On a phone the answer to a gate sits in a bar pinned above the tab bar (#178).
const ts = "2026-10-26T10:00:00+00:00";
const world = { id: "world_1", name: "Video", purpose: "", created_at: ts };

function idea(rank: number, title: string): ArtifactFile {
  return {
    name: `idea-${rank}.md`,
    path: `runs/run_1/stage_0/artifacts/idea-${rank}.md`,
    type: "idea",
    author: "erlich",
    title,
    fields: { rank, hook: `${title}, in one line.` },
    body: "",
    run_id: "run_1",
    stage: "stage_0",
    created: ts,
    pinned: false,
  };
}
const ideas = [idea(1, "Why a cast saw can't cut your skin"), idea(2, "The cereal fired out of cannons"), idea(3, "Hot cars")];
const video: ArtifactFile = { ...idea(1, "video.mp4"), name: "video.mp4", path: "runs/run_1/stage_2/artifacts/video.mp4", type: "video", stage: "stage_2", fields: {} };

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(390, 844);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** On screen without scrolling: inside the viewport and clear of the tab bar under it. */
function inReach(el: Element) {
  const bar = el.getBoundingClientRect();
  const tabs = document.querySelector("nav.tabbar")!.getBoundingClientRect();
  expect(window.scrollY).toBe(0);
  expect(bar.top).toBeGreaterThan(0);
  expect(bar.bottom).toBeLessThanOrEqual(tabs.top);
}

test("on a phone Needs you shows the queue as chips and the pick's answer in reach, named once chosen", async () => {
  let body: unknown;
  fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/gates": () =>
      Response.json([gate("pick", { stage_id: "s1" }), gate("approve", { run_id: "run_2", stage_id: "s2" })]),
    "GET /api/runs/run_1/artifacts": () => Response.json(ideas),
    "POST /api/runs/run_1/decision": async (req) => {
      body = await req.json();
      return Response.json(run("running", [stage(0, "x", "connector", "running")]));
    },
  });
  const { screen } = await mountAt("/gates");
  const chips = screen.getByRole("navigation", { name: "Waiting decisions" });
  // What shows: the place in the queue and the kind; the world and run stay in the card below.
  await vi.waitFor(() =>
    expect([...chips.element().querySelectorAll("a")].map((a) => a.innerText)).toEqual(["1 of 2 · Pick an idea", "Review and approve"]),
  );

  const card = screen.getByTestId("gate-run_1");
  const primary = card.getByRole("button", { name: "Continue", exact: true });
  await expect.element(primary).toBeDisabled();
  inReach(card.element().querySelector(".gate-actions")!);

  await card.getByText("The cereal fired out of cannons").click();
  await expect.element(card.getByRole("button", { name: "Continue with #2" })).toBeEnabled();
  await card.getByRole("button", { name: "Continue with #2" }).click();
  await vi.waitFor(() =>
    expect(body).toEqual({ note: "", choice: "The cereal fired out of cannons", artifact: ideas[1]!.path }),
  );
});

test("on a phone one waiting decision shows no chips", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/gates": () => Response.json([gate("confirm", { stage_id: "s3" })]),
  });
  const { screen } = await mountAt("/gates");
  await expect.element(screen.getByTestId("gate-run_1").getByRole("button", { name: "Confirm" })).toBeEnabled();
  expect(screen.getByRole("navigation", { name: "Waiting decisions" }).query()).toBeNull();
});

test("on a phone a run waiting at an approve gate keeps Send back and Approve in reach under the video", async () => {
  const waiting = run("waiting_gate", [
    stage(0, "research", "swarm", "done"),
    stage(1, "review", "gate", "waiting", { ask: "Watch it. Approve or send back." }),
  ]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "GET /api/runs/run_1/artifacts": () => Response.json([video]),
    "GET /api/gates": () => Response.json([gate("approve", { earlier_stages: ["research"] })]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  const panel = screen.getByTestId("stage-panel");
  await expect.element(panel.getByTestId("player")).toBeInTheDocument();
  const approve = panel.getByRole("button", { name: "Approve" });
  await expect.element(approve).toBeEnabled();
  const back = panel.getByRole("button", { name: "Send back to..." });
  await expect.element(back).toHaveTextContent("Send back…");
  inReach(approve.element().closest(".gate-actions")!);
  // Send back sits before the primary, as the thumb reads it.
  expect(back.element().getBoundingClientRect().left).toBeLessThan(approve.element().getBoundingClientRect().left);
  // The video is capped, so the run does not open on a full screen of it.
  expect(panel.getByTestId("player").element().getBoundingClientRect().height).toBeLessThanOrEqual(300);
  // Its menu opens upward, above the bar.
  await back.click();
  const menu = panel.getByRole("menu").element().getBoundingClientRect();
  expect(menu.bottom).toBeLessThanOrEqual(back.element().getBoundingClientRect().top);
});
