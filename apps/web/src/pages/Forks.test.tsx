import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const video = { id: "world_1", name: "Video", purpose: "", created_at: ts };
const source = { id: "run_1", world_id: "world_1", name: "Render then pick" };
const forked = { id: "run_2", world_id: "world_1", name: "Render then pick" };
const done = run("done", [
  stage(0, "research", "swarm", "done"),
  stage(1, "pick", "gate", "done"),
  stage(2, "render", "connector", "done"),
]);

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a finished stage offers a fork, which posts the stage and opens the new run", async () => {
  let body: unknown = null;
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "POST /api/runs/run_1/fork": async (req) => {
      body = await req.json();
      return Response.json({ ...done, id: "run_2", status: "queued" }, { status: 201 });
    },
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_1");
  // The selected step offers it, one at a time.
  expect(screen.getByRole("button", { name: /Fork the run from/ }).elements()).toHaveLength(1);
  await screen.getByRole("button", { name: "Fork the run from pick" }).click();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_2"));
  expect(body).toEqual({ stage: 1 });
});

test("only stages that finished, after finished stages, can be forked from", async () => {
  const waiting = run("waiting_gate", [
    stage(0, "research", "swarm", "done"),
    stage(1, "pick", "gate", "waiting"),
    stage(2, "render", "connector", "pending"),
  ]);
  fakeFetch({ "GET /api/runs/run_1": () => Response.json(waiting) });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/^pick/);
  expect(screen.getByRole("button", { name: /Fork the run from/ }).elements()).toHaveLength(0);
  await screen.getByRole("button", { name: /^Step 1, research/ }).click();
  await expect.element(screen.getByRole("button", { name: "Fork the run from research" })).toBeVisible();
  await screen.getByRole("button", { name: /^Step 3, render/ }).click();
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/^render/);
  expect(screen.getByRole("button", { name: /Fork the run from/ }).elements()).toHaveLength(0);
});

test("a refused fork shows the API's reason", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "POST /api/runs/run_1/fork": () => Response.json({ detail: "stage pick has not finished" }, { status: 409 }),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await screen.getByRole("button", { name: "Fork the run from render" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("stage pick has not finished");
});

test("a fork names its source and the source lists its forks; a promote names its artifact", async () => {
  const fork = run("queued", done.stages, { ...forked, forked_from: source, fork_stage_index: 1 });
  const parent = run("done", done.stages, { forks: [forked] });
  const promoted = run("queued", done.stages, { id: "run_3", promoted_from: "runs/run_1/stage_0/artifacts/ideas.md" });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(parent),
    "GET /api/runs/run_2": () => Response.json(fork),
    "GET /api/runs/run_3": () => Response.json(promoted),
  });
  const forkPage = await mountAt("/worlds/world_1/runs/run_2");
  await expect
    .element(forkPage.screen.getByTestId("forked-from"))
    .toMatchTextContent(/Forked from Render then pick at stage 2\. The earlier stages were copied, not run again\./);
  await expect
    .element(forkPage.screen.getByTestId("forked-from").getByRole("link"))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_1");

  const parentPage = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(parentPage.screen.getByTestId("forks")).toMatchTextContent(/Forks: Render then pick/);
  await expect
    .element(parentPage.screen.getByTestId("forks").getByRole("link"))
    .toHaveAttribute("href", "/worlds/world_1/runs/run_2");

  const promotedPage = await mountAt("/worlds/world_1/runs/run_3");
  await expect
    .element(promotedPage.screen.getByTestId("promoted-from"))
    .toMatchTextContent(/Promoted from ideas\.md in the library; the skipped stages never ran\./);
  await expect
    .element(promotedPage.screen.getByRole("link", { name: "library" }))
    .toHaveAttribute("href", "/worlds/world_1/library");
});

test("the runs page labels a fork with its source", async () => {
  const fork = run("queued", done.stages, { ...forked, forked_from: source, fork_stage_index: 1 });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/runs": () => Response.json([fork, run("done", done.stages)]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs");
  await expect.element(screen.getByTestId("fork-of")).toMatchTextContent("fork of Render then pick");
  await expect.element(screen.getByTestId("fork-of").getByRole("link")).toHaveAttribute("href", "/worlds/world_1/runs/run_1");
  expect(screen.getByTestId("fork-of").elements()).toHaveLength(1);
});
