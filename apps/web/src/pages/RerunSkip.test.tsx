import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:02:00+00:00";
const waiting = run(
  "waiting_gate",
  [
    stage(0, "script", "swarm", "done", { started_at: ts, ended_at: ts }),
    stage(1, "pick", "gate", "waiting", { started_at: ts }),
    stage(2, "confirm", "gate", "pending"),
    stage(3, "upload", "connector", "pending", { calls: "youtube" }),
  ],
  { revision: 3 },
);
const applied = (kind: string, index: number, name: string, payload = {}) => ({
  id: "change_1", run_id: "run_1", kind, stage_id: `stage_${index}`, stage_index: index, stage_name: name, payload, before: {}, by: "operator", at: ts, revision: 4,
});

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("Re-run from here shows what it resets and costs against the budget left, applies with a note and marks the step", async () => {
  const sent: unknown[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      const before = { script: "done", pick: "waiting" };
      return Response.json({
        change, stage_name: "script", before, after: { script: "pending", pick: "pending" }, violations: [], revision: 3, in_place: true, cost_usd: 3.1, budget_left_usd: 0.82,
      });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      sent.push(body);
      return Response.json(applied("rerun", 0, "script", body.payload), { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByRole("button", { name: "Run actions" }).click();
  expect(screen.getByRole("menuitem", { name: /^Skip/ }).elements()).toHaveLength(0);
  await screen.getByRole("menuitem", { name: /^Re-run from here/ }).click();

  const card = screen.getByTestId("change-card");
  await expect.element(card.getByRole("heading", { name: "Re-run from script" })).toBeVisible();
  await expect.element(card.getByRole("list", { name: "The change" })).toHaveTextContent("script done pendingpick waiting pending");
  await expect.element(card).toMatchTextContent(/Resets script and pick, about \$3\.10 more of the \$0\.82 left/);
  await card.getByLabelText("Note for script").fill("A stronger hook.");
  await card.getByRole("button", { name: "Apply" }).click();
  await vi.waitFor(() => expect(sent).toEqual([{ kind: "rerun", stage: "stage_0", payload: { note: "A stronger hook." }, revision: 3 }]));
  await expect.element(card).toMatchTextContent(/Applied by you at \d\d:\d\d/);

  FakeEventSource.emit("run.updated", { payload: { ...waiting, revision: 4, changes: [applied("rerun", 0, "script")], updated_at: "2026-10-26T10:04:00+00:00" } });
  await expect.element(screen.getByTestId("timeline")).toMatchTextContent(/script.*Re-run by you \d\d:\d\d/);
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Changes \(1\)/ }).click();
  await expect.element(screen.getByTestId("changes")).toMatchTextContent(/Re-run from script · by you at \d\d:\d\d/);
});

test("a Rerun while the run is at work shows the refusal and Apply stays off", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "POST /api/runs/run_1/changes/propose": async (req) =>
      Response.json({ change: await req.json(), stage_name: "script", before: { script: "done" }, after: { script: "pending" }, violations: ["the run is at work: pause first"], revision: 3, in_place: true, cost_usd: 0, budget_left_usd: null }),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Re-run from here/ }).click();
  const card = screen.getByTestId("change-card");
  await expect.element(card).toMatchTextContent(/This would not pass: the run is at work: pause first\./);
  await expect.element(card).toMatchTextContent(/Resets script, about \$0\.00 more/);
  await expect.element(card).not.toMatchTextContent(/ left/);
  await expect.element(card.getByRole("button", { name: "Apply" })).toBeDisabled();
});

test("Skip on a step that has not started applies, and one the check refuses keeps Apply off", async () => {
  const sent: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      const name = change.stage === "stage_2" ? "confirm" : "upload";
      const violations = name === "confirm" ? ["publish connector 'youtube' must come right after a gate"] : [];
      return Response.json({ change, stage_name: name, before: { [name]: "pending" }, after: { [name]: "skipped" }, violations, revision: 3, in_place: true, cost_usd: null, budget_left_usd: null });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      sent.push(body);
      return Response.json(applied("skip", 3, "upload"), { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_2");
  await screen.getByRole("button", { name: "Run actions" }).click();
  expect(screen.getByRole("menuitem", { name: /^Re-run from here/ }).elements()).toHaveLength(0);
  await screen.getByRole("menuitem", { name: /^Skip/ }).click();
  let card = screen.getByTestId("change-card");
  await expect.element(card.getByRole("heading", { name: "Skip confirm" })).toBeVisible();
  await expect.element(card).toMatchTextContent(/This would not pass: publish connector 'youtube' must come right after a gate\./);
  await expect.element(card.getByRole("button", { name: "Apply" })).toBeDisabled();
  await card.getByRole("button", { name: "Not now" }).click();
  await expect.element(screen.getByTestId("change-card")).not.toBeInTheDocument();

  await screen.getByRole("button", { name: /^Step 4, upload/ }).click();
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Skip/ }).click();
  card = screen.getByTestId("change-card");
  await expect.element(card.getByRole("list", { name: "The change" })).toHaveTextContent("upload pending skipped");
  expect(card.getByRole("button", { name: "Edit" }).elements()).toHaveLength(0);
  await card.getByRole("button", { name: "Apply" }).click();
  await vi.waitFor(() => expect(sent).toEqual([{ kind: "skip", stage: "stage_3", payload: {}, revision: 3 }]));
  await expect.element(card).toMatchTextContent(/Applied by you at \d\d:\d\d/);
  expect(calls.filter((c) => c === "POST /api/runs/run_1/changes")).toHaveLength(1);
});
