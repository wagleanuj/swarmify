import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import type { components } from "../api/schema";
import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { trendLine } from "./Scorecard";

type Score = components["schemas"]["RunScore"];

const score = (run_id: string, created: string, over: Partial<Score> = {}): Score => ({
  run_id,
  name: "Short",
  workflow_version: 1,
  baseline: false,
  status: "done",
  created_at: created,
  judged: 2,
  first_try: 1,
  send_backs: 1,
  cost_usd: 0.5,
  duration_s: 300,
  outcomes: {},
  ...over,
});
const day = (n: number) => `2026-10-${String(n).padStart(2, "0")}T10:00:00+00:00`;
const runs = [
  score("run_1", day(1), { first_try: 0, send_backs: 2, cost_usd: 0.8, outcomes: { views: 1200, clicks: 12 } }),
  score("run_2", day(2), { baseline: true, judged: 0, first_try: 0, send_backs: 0, cost_usd: 0.3 }),
  score("run_3", day(3)),
  score("run_4", day(4), { first_try: 2, send_backs: 0, cost_usd: 0.4, workflow_version: 2 }),
  score("run_5", day(5), { first_try: 2, send_backs: 0, cost_usd: 0.4, workflow_version: 2, status: "running", duration_s: null }),
  score("run_6", day(6), { first_try: 2, send_backs: 0, cost_usd: 0.4, workflow_version: null }),
];

// The editor is read-only below 48rem; these read its desktop Scorecard tab.
beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const editor = {
  "GET /api/workflows/wf_1/versions": () => Response.json([]),
  "POST /api/worlds/world_1/workflows/check": () => Response.json({ violations: [], model: null }),
};

test("the old scorecard address opens the editor on its Scorecard tab: one row per run, plus the trend", async () => {
  fakeFetch({
    ...editor,
    "GET /api/workflows/wf_1/scorecard": () =>
      Response.json({ workflow_id: "wf_1", world_id: "world_1", name: "Short", runs }),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/wf_1/scorecard");
  await expect.element(screen.getByRole("tab", { name: "Scorecard" })).toHaveAttribute("aria-selected", "true");
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_1");
  expect(router.state.location.search).toEqual({ tab: "scorecard" });
  const rows = screen.getByTestId("score");
  expect(rows.elements()).toHaveLength(6);
  await expect.element(rows.first()).toMatchTextContent(/v1.*done.*0 of 2.*2.*\$0\.80.*5 min.*1,200 views, 12 clicks/);
  await expect.element(rows.nth(1)).toMatchTextContent(/baseline.*none judged.*not measured/);
  await expect.element(rows.nth(4)).toMatchTextContent(/running.*running/);
  await expect.element(rows.nth(5)).toMatchTextContent(/ad hoc/);
  await expect.element(rows.first().getByRole("link")).toHaveAttribute("href", "/worlds/world_1/runs/run_1");
  await expect
    .element(screen.getByTestId("trend"))
    .toHaveTextContent("Last 3 runs against the 2 before: first-try acceptance 100% (was 25%), 0.0 send-backs a run (was 1.5), $0.40 a run (was $0.65), 5 min a run (was 5 min).");
});

test("the trend needs more than three swarm runs and the empty state says what lands here", async () => {
  expect(trendLine(runs.slice(0, 4))).toBeNull();
  expect(trendLine(runs.slice(0, 5))).toMatch(/^Last 3 runs against the 1 before/);
  fakeFetch({
    ...editor,
    "GET /api/workflows/wf_1/scorecard": () =>
      Response.json({ workflow_id: "wf_1", world_id: "world_1", name: "Short", runs: [] }),
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/wf_1?tab=scorecard");
  await expect.element(screen.getByTestId("no-scores")).toMatchTextContent(/No runs yet/);
  await expect
    .element(screen.getByRole("link", { name: "Start a run" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows");
});
