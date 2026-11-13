import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "./index.css";

import { armed, intent, PULL, pullDistance } from "./PullToRefresh";
import { startResync, STALE_MS } from "./refresh";
import { FakeEventSource, fakeFetch, run, stage } from "./test/fakes";
import { mountAt } from "./test/mount";

test("the spinner follows half the pull, stiffens past the threshold and stops at its max", () => {
  expect(pullDistance(-20)).toBe(0);
  expect(pullDistance(100)).toBe(50);
  expect(armed(pullDistance(PULL.threshold * 2 - 1))).toBe(false);
  expect(armed(pullDistance(PULL.threshold * 2))).toBe(true);
  // Past the threshold the same finger travel moves it less.
  expect(pullDistance(PULL.threshold * 2 + 100) - PULL.threshold).toBeLessThan(50);
  expect(pullDistance(10_000)).toBe(PULL.max);
});

test("only a mostly downward move is a pull: a sideways swipe or a scroll up is not", () => {
  expect(intent(3, 4)).toBeNull();
  expect(intent(2, 20)).toBe("pull");
  expect(intent(30, 20)).toBe("other"); // the Needs-you deck's swipe
  expect(intent(0, -20)).toBe("other");
});

const ts = "2026-10-26T10:00:00+00:00";

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(390, 844);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("back after 30 s away, the page refetches and its stream reopens from the new snapshot", async () => {
  let seq = 5;
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json({ id: "world_1", name: "Video", purpose: "", created_at: ts }),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([run("running", [stage(0, "s0", "connector", "running")])], { headers: { "X-Event-Seq": String(seq) } }),
  });
  let clock = 0;
  startResync(() => clock);
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect.element(screen.getByTestId("run-list")).toBeVisible();
  await vi.waitUntil(() => FakeEventSource.open.some((s) => s.url.endsWith("/api/worlds/world_1/events?since=5")));
  const loads = () => calls.filter((c) => c === "GET /api/worlds/world_1/runs").length;
  const before = loads();

  const visibility = vi.spyOn(document, "visibilityState", "get");
  const flip = (state: DocumentVisibilityState) => {
    visibility.mockReturnValue(state);
    document.dispatchEvent(new Event("visibilitychange"));
  };
  // A short trip away is not worth a refetch.
  flip("hidden");
  clock += 5_000;
  flip("visible");
  await new Promise((r) => setTimeout(r, 200));
  expect(loads()).toBe(before);

  seq = 9;
  flip("hidden");
  clock += STALE_MS + 1;
  flip("visible");
  await vi.waitUntil(() => loads() > before);
  // The old stream is closed and one opens from where the new snapshot stands.
  await vi.waitUntil(() => FakeEventSource.open.some((s) => s.url.endsWith("since=9")));
  expect(FakeEventSource.open.filter((s) => s.url.includes("/worlds/world_1/events"))).toHaveLength(1);
});
