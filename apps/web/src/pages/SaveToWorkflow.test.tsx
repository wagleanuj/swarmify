import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { artifact, FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:02:00+00:00";
const render = stage(1, "render", "connector", "pending", { calls: "remotion_render", config: { calls: "remotion_render", params: { fps: 30 } } });
const waiting = run("waiting_gate", [stage(0, "pick", "gate", "waiting", { started_at: ts }), render], { revision: 3 });
const v2 = "name: Render then pick\nstages:\n  - connector: render\n    calls: remotion_render\n    params:\n      fps: 30\n";

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function draft(over: Record<string, unknown> = {}) {
  return { stage: "render", workflow_id: "wf_1", version: 3, run_version: 2, before: v2, yaml: v2.replace("fps: 30", "fps: 60"), violations: [], conflicts: [], saved: null, ...over };
}

test("an applied Stage Edit drafts the workflow's next version, names a newer version's change and saves only once confirmed twice", async () => {
  const asked: Record<string, unknown>[] = [];
  const conflicts = ["Since this run's v2, v3 changed render (output)."];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      return Response.json({ change, stage_name: "render", before: render.config, after: change.payload, violations: [], revision: 3, in_place: true });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const { kind, stage: stageId, payload } = await req.json();
      return Response.json({ id: "change_1", run_id: "run_1", kind, stage_id: stageId, stage_index: 1, stage_name: "render", payload, before: render.config, by: "operator", at: ts, revision: 4 }, { status: 201 });
    },
    "POST /api/runs/run_1/changes/change_1/save-to-workflow": async (req) => {
      const body = await req.json();
      asked.push(body);
      const saved = body.save ? { id: "wfv_4", workflow_id: "wf_1", number: 4, yaml: "", created_at: ts, model: { name: "x", stages: [] } } : null;
      return Response.json(draft({ conflicts, saved }));
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Edit stage/ }).click();
  const card = screen.getByTestId("change-card");
  await card.getByLabelText("render's config").fill("calls: remotion_render\nparams:\n  fps: 60\n");
  const apply = card.getByRole("button", { name: "Apply" });
  await expect.element(apply).toBeEnabled();
  await apply.click();
  await expect.element(card).toMatchTextContent(/Applied by you at \d\d:\d\d/);

  await card.getByRole("button", { name: "Also save to the workflow" }).click();
  const save = card.getByRole("region", { name: "Save to the workflow" });
  await expect.element(save).toMatchTextContent(/Saves the change to render as v4 of the workflow\. This run keeps v2\./);
  const diff = save.getByRole("list", { name: "The workflow change" });
  await expect.element(diff.getByRole("deletion")).toHaveTextContent("fps: 30");
  await expect.element(diff.getByRole("insertion")).toHaveTextContent("fps: 60");
  await expect.element(save).toMatchTextContent(/Since this run's v2, v3 changed render \(output\)\./);
  expect(asked).toEqual([{ save: false, confirmed: false }]);

  // A newer version changed the stage: the first Save asks again, the second saves over it.
  await save.getByRole("button", { name: "Save as v4" }).click();
  await expect.element(save).toMatchTextContent(/The workflow changed since this run's version\./);
  expect(asked).toHaveLength(1);
  await save.getByRole("button", { name: "Save anyway" }).click();
  await expect.element(card).toMatchTextContent(/Saved as v4 of the workflow\./);
  expect(asked.at(-1)).toEqual({ save: true, confirmed: true, version: 3 });
});

test("an applied Artifact Edit saves the Run Assistant's drafted Stage Edit to the stage that made the file", async () => {
  const seven = "#a #b #c #d #e #f #g\n";
  const five = "#a #b #c #d #e\n";
  const done = { started_at: ts, ended_at: ts };
  const stages = [stage(0, "write", "swarm", "done", done), stage(1, "confirm", "gate", "waiting", { started_at: ts })];
  const metadata = artifact({ name: "metadata.md", path: "runs/run_1/stage_0/artifacts/metadata.md", type: "metadata", title: "metadata.md", body: seven });
  const config = { goal: "Write the metadata with at most five hashtags." };
  const asked: Record<string, unknown>[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("waiting_gate", stages, { revision: 7 })),
    "GET /api/runs/run_1/artifacts": () => Response.json([metadata]),
    "GET /api/worlds/world_1/files/runs/run_1/stage_0/artifacts/metadata.md": () => new Response(seven),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      return Response.json({ change, stage_name: "write", before: { path: "metadata.md", text: seven }, after: change.payload, violations: [], revision: 7, in_place: true });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      return Response.json({ id: "change_1", run_id: "run_1", kind: body.kind, stage_id: "stage_0", stage_index: 0, stage_name: "write", payload: body.payload, before: {}, by: "operator", at: ts, revision: 8 }, { status: 201 });
    },
    "POST /api/runs/run_1/changes/change_1/save-to-workflow": async (req) => {
      const body = await req.json();
      asked.push(body);
      const saved = body.save ? { id: "wfv_3", workflow_id: "wf_1", number: 3, yaml: "", created_at: ts, model: { name: "x", stages: [] } } : null;
      const goal = "  - swarm: write\n    goal: Write the metadata.\n";
      return Response.json(draft({ stage: "write", version: 2, before: goal, yaml: goal.replace("metadata.", "metadata with at most five hashtags."), config, reply: "Cap the hashtags in write's goal.", saved }));
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByTestId("all-files").getByText("All files (1)").click();
  await screen.getByRole("button", { name: "Edit file metadata.md" }).click();
  const card = screen.getByTestId("change-card");
  await card.getByLabelText("metadata.md's text").fill(five);
  const apply = card.getByRole("button", { name: "Apply" });
  await expect.element(apply).toBeEnabled();
  await apply.click();

  await card.getByRole("button", { name: "Also save to the workflow" }).click();
  const save = card.getByRole("region", { name: "Save to the workflow" });
  await expect.element(save).toMatchTextContent(/Cap the hashtags in write's goal\./);
  await expect.element(save.getByRole("insertion")).toHaveTextContent("goal: Write the metadata with at most five hashtags.");
  // The drafted Stage Edit is the operator's to change; it is drafted once, kept through "Not now".
  const edited = { goal: "Write the metadata with at most three hashtags." };
  await save.getByLabelText("The Stage Edit to write").fill("goal: Write the metadata with at most three hashtags.\n");
  await expect.poll(() => asked.length).toBe(2);
  expect(asked[1]).toEqual({ save: false, confirmed: false, config: edited });
  await save.getByRole("button", { name: "Not now" }).click();
  await card.getByRole("button", { name: "Also save to the workflow" }).click();
  await expect.element(save.getByLabelText("The Stage Edit to write")).toHaveValue("goal: Write the metadata with at most three hashtags.\n");
  expect(asked[2]).toEqual({ save: false, confirmed: false, config: edited });
  await save.getByRole("button", { name: "Save as v3" }).click();
  await expect.element(card).toMatchTextContent(/Saved as v3 of the workflow\./);
  expect(asked.at(-1)).toEqual({ save: true, confirmed: false, version: 2, config: edited });
});

test("a save refused because the workflow moved on shows the draft as it now stands", async () => {
  const asked: Record<string, unknown>[] = [];
  const conflicts = ["Since this run's v2, v4 changed render (output)."];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      return Response.json({ change, stage_name: "render", before: render.config, after: change.payload, violations: [], revision: 3, in_place: true });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const { kind, stage: stageId, payload } = await req.json();
      return Response.json({ id: "change_1", run_id: "run_1", kind, stage_id: stageId, stage_index: 1, stage_name: "render", payload, before: render.config, by: "operator", at: ts, revision: 4 }, { status: 201 });
    },
    "POST /api/runs/run_1/changes/change_1/save-to-workflow": async (req) => {
      const body = await req.json();
      asked.push(body);
      if (body.save) return Response.json({ detail: "The workflow moved on since this draft; draft it again." }, { status: 409 });
      return Response.json(asked.length === 1 ? draft() : draft({ version: 4, conflicts }));
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Edit stage/ }).click();
  const card = screen.getByTestId("change-card");
  await card.getByLabelText("render's config").fill("calls: remotion_render\nparams:\n  fps: 60\n");
  const apply = card.getByRole("button", { name: "Apply" });
  await expect.element(apply).toBeEnabled();
  await apply.click();
  await card.getByRole("button", { name: "Also save to the workflow" }).click();
  const save = card.getByRole("region", { name: "Save to the workflow" });
  await save.getByRole("button", { name: "Save as v4" }).click();
  await expect.element(save.getByRole("alert")).toMatchTextContent(/moved on since this draft/);
  await expect.element(save).toMatchTextContent(/Since this run's v2, v4 changed render \(output\)\./);
  await expect.element(save.getByRole("button", { name: "Save as v5" })).toBeEnabled();
  expect(asked).toEqual([{ save: false, confirmed: false }, { save: true, confirmed: false, version: 3 }, { save: false, confirmed: false }]);
});
