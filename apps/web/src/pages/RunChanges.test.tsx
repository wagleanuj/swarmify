import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { artifact, FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:02:00+00:00";
const render = stage(1, "render", "connector", "pending", { calls: "remotion_render", config: { calls: "remotion_render", params: { fps: 30 } } });
const waiting = run("waiting_gate", [stage(0, "pick", "gate", "waiting", { started_at: ts }), render], { revision: 3 });

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a Stage Edit is proposed, shows what the check refuses, goes stale when the run moves, applies and marks the stage Edited", async () => {
  let revision = 3;
  const applied: unknown[] = [];
  const calls = fakeFetch({
    "GET /api/runs/run_1": () => Response.json(waiting),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      const violations = change.payload.calls === "nobody" ? ["no connector named 'nobody'"] : [];
      return Response.json({ change, stage_name: "render", before: render.config, after: change.payload, violations, revision, in_place: true });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      applied.push(body);
      const { kind, stage: stageId, payload } = body;
      return Response.json(
        { id: "change_1", run_id: "run_1", kind, stage_id: stageId, stage_index: 1, stage_name: "render", payload, before: render.config, by: "operator", at: ts, revision: 5 },
        { status: 201 },
      );
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Edit stage/ }).click();

  const card = screen.getByTestId("change-card");
  await expect.element(card.getByRole("heading", { name: "Edit render" })).toBeVisible();
  const config = card.getByLabelText("render's config");
  await expect.element(config).toHaveValue("calls: remotion_render\nparams:\n  fps: 30\n");
  const apply = card.getByRole("button", { name: "Apply" });
  await expect.element(apply).toBeDisabled();

  // What the check would refuse shows on the card, and Apply waits until it is fixed.
  await config.fill("calls: nobody\nparams:\n  fps: 30\n");
  await expect.element(card).toMatchTextContent(/This would not pass: no connector named 'nobody'\./);
  await expect.element(apply).toBeDisabled();

  await config.fill("calls: remotion_render\nparams:\n  fps: 60\n");
  await expect.element(card.getByRole("list", { name: "The change" })).toHaveTextContent('params {"fps":30} {"fps":60}');
  await expect.element(card).not.toMatchTextContent(/This would not pass/);
  await expect.element(apply).toBeEnabled();

  // The run moved on (the gate was answered elsewhere): the proposal is stale until asked again.
  revision = 4;
  FakeEventSource.emit("run.updated", { payload: { ...waiting, revision: 4, updated_at: "2026-10-26T10:03:00+00:00" } });
  await expect.element(card).toMatchTextContent(/The run moved on since this was proposed\./);
  await expect.element(apply).toBeDisabled();
  await card.getByRole("button", { name: "Ask again" }).click();
  await expect.element(card).not.toMatchTextContent(/The run moved on/);
  await apply.click();
  await vi.waitFor(() =>
    expect(applied).toEqual([{ kind: "stage_edit", stage: "stage_1", payload: { calls: "remotion_render", params: { fps: 60 } }, revision: 4 }]),
  );
  await expect.element(card).toMatchTextContent(/Applied by you at \d\d:\d\d/);
  expect(calls.filter((c) => c === "POST /api/runs/run_1/changes")).toHaveLength(1);

  // The run's own update carries the change: the Timeline marks the stage, the menu counts it.
  const change = { id: "change_1", run_id: "run_1", kind: "stage_edit", stage_id: "stage_1", stage_index: 1, stage_name: "render", payload: {}, before: {}, by: "operator", at: ts, revision: 5 };
  FakeEventSource.emit("run.updated", { payload: { ...waiting, revision: 5, changes: [change], updated_at: "2026-10-26T10:04:00+00:00" } });
  await expect.element(screen.getByTestId("timeline")).toMatchTextContent(/render\s*video · Edited by you \d\d:\d\d/);
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Changes \(1\)/ }).click();
  await expect.element(screen.getByTestId("changes")).toMatchTextContent(/Edited render · by you at \d\d:\d\d/);
});

test("Not now closes the card with nothing applied, and a stage that has started offers no edit", async () => {
  const calls = fakeFetch({ "GET /api/runs/run_1": () => Response.json(waiting) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  await screen.getByRole("button", { name: "Run actions" }).click();
  await screen.getByRole("menuitem", { name: /^Edit stage/ }).click();
  await screen.getByTestId("change-card").getByRole("button", { name: "Not now" }).click();
  await expect.element(screen.getByTestId("change-card")).not.toBeInTheDocument();
  expect(calls).not.toContain("POST /api/runs/run_1/changes");

  await screen.getByRole("button", { name: /^Step 1, pick/ }).click();
  await screen.getByRole("button", { name: "Run actions" }).click();
  await expect.element(screen.getByRole("menuitem", { name: /^Pause/ })).toBeVisible();
  expect(screen.getByRole("menuitem", { name: /^Edit stage/ }).elements()).toHaveLength(0);
});

test("an Artifact Edit of a finished step's text file shows the line diff and who read it, refuses a bad type, applies and links the earlier version", async () => {
  const seven = '---\ntype: "metadata"\n---\n\n#a #b #c #d #e #f #g\n';
  const five = '---\ntype: "metadata"\n---\n\n#a #b #c #d #e\n';
  const done = { started_at: ts, ended_at: ts };
  const stages = [
    stage(0, "write", "connector", "done", { calls: "tagger", ...done }),
    stage(1, "check", "connector", "done", { calls: "tagger", inputs: ["metadata.md"], ...done }),
    stage(2, "confirm", "gate", "waiting", { started_at: ts }),
    stage(3, "upload", "connector", "pending", { calls: "tagger", inputs: ["metadata.md"] }),
  ];
  const tagged = run("waiting_gate", stages, { revision: 7 });
  const metadata = artifact({ name: "metadata.md", path: "runs/run_1/stage_0/artifacts/metadata.md", type: "metadata", title: "metadata.md", body: "#a #b #c #d #e #f #g\n" });
  const video = artifact({ name: "short.mp4", path: "runs/run_1/stage_0/artifacts/short.mp4.md", type: "video", title: "short.mp4" });
  const applied: unknown[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(tagged),
    "GET /api/runs/run_1/artifacts": () => Response.json([metadata, video]),
    "GET /api/worlds/world_1/files/runs/run_1/stage_0/artifacts/metadata.md": () => new Response(seven),
    "POST /api/runs/run_1/changes/propose": async (req) => {
      const change = await req.json();
      const violations = change.payload.text.includes("#h") ? ["metadata.md no longer fits type metadata at tags: the list is too long"] : [];
      const before = { path: "metadata.md", text: seven };
      return Response.json({ change, stage_name: "write", before, after: change.payload, violations, revision: 7, in_place: true, read_by: ["check"] });
    },
    "POST /api/runs/run_1/changes": async (req) => {
      const body = await req.json();
      applied.push(body);
      const before = { path: "metadata.md", text: seven, kept: "runs/run_1/stage_0/previous/1-metadata.md" };
      return Response.json(
        { id: "change_1", run_id: "run_1", kind: body.kind, stage_id: "stage_0", stage_index: 0, stage_name: "write", payload: body.payload, before, by: "operator", at: ts, revision: 8 },
        { status: 201 },
      );
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByTestId("all-files").getByText("All files (2)").click();
  expect(screen.getByRole("button", { name: "Edit file short.mp4" }).elements()).toHaveLength(0);
  await screen.getByRole("button", { name: "Edit file metadata.md" }).click();

  const card = screen.getByTestId("change-card");
  await expect.element(card.getByRole("heading", { name: "Edit metadata.md" })).toBeVisible();
  const text = card.getByLabelText("metadata.md's text");
  await expect.element(text).toHaveValue(seven);
  const apply = card.getByRole("button", { name: "Apply" });
  await expect.element(apply).toBeDisabled();

  await text.fill(seven.replace("#g", "#g #h"));
  await expect.element(card).toMatchTextContent(/This would not pass: metadata.md no longer fits type metadata at tags: the list is too long\./);
  await expect.element(apply).toBeDisabled();

  await text.fill(five);
  const diff = card.getByRole("list", { name: "The change" });
  await expect.element(diff.getByRole("deletion")).toHaveTextContent("#a #b #c #d #e #f #g");
  await expect.element(diff.getByRole("insertion")).toHaveTextContent("#a #b #c #d #e");
  await expect.element(card).toMatchTextContent(/check already read the old version\. Re-run it\?/);
  await expect.element(apply).toBeEnabled();
  await apply.click();
  await vi.waitFor(() => expect(applied).toEqual([{ kind: "artifact_edit", stage: "stage_0", payload: { path: "metadata.md", text: five }, revision: 7 }]));
  await expect.element(card).toMatchTextContent(/Applied by you at \d\d:\d\d · Earlier version/);
  await expect.element(card.getByRole("link", { name: "Earlier version" })).toHaveAttribute("href", expect.stringContaining("previous%2F1-metadata.md"));
});
