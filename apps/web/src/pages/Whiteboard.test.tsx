import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch, run, stage, task } from "../test/fakes";
import { mountAt } from "../test/mount";

const later = "2026-10-26T10:01:00+00:00";
const researching = run("running", [
  stage(0, "research", "swarm", "running"),
  stage(1, "pick", "gate", "pending"),
]);
const TASKS = "GET /api/runs/run_1/stages/stage_0/tasks";

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the swarm stage's whiteboard shows its columns and follows the world stream", async () => {
  const pull = task("task_1", "open", { title: "Pull top shorts" });
  const rank = task("task_2", "open", { title: "Rank hooks", depends_on: ["task_1"] });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    [TASKS]: () => Response.json([pull, rank]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const board = screen.getByTestId("whiteboard");
  await expect.element(board.getByRole("heading", { level: 2 })).toHaveTextContent("Whiteboard · research");
  await expect.element(screen.getByTestId("col-open")).toMatchTextContent(/Open 2.*Pull top shorts.*added by richard/);
  await expect.element(screen.getByTestId("col-open")).toMatchTextContent(/Rank hooks.*waiting on 1 task/);
  await expect.element(screen.getByTestId("col-claimed")).toHaveTextContent("Claimed 0Nothing in flight.");
  expect(FakeEventSource.open).toHaveLength(1);

  FakeEventSource.emit("task.updated", {
    payload: task("task_1", "claimed", { title: "Pull top shorts", updated_at: later }),
  });
  await expect.element(screen.getByTestId("col-claimed")).toMatchTextContent(/Claimed 1.*Pull top shorts.*claimed by gilfoyle/);
  await expect.element(screen.getByTestId("whiteboard-summary")).toHaveTextContent("1 open, 1 claimed, 0 done.");

  // A task of another stage is not this board's business.
  FakeEventSource.emit("task.updated", { payload: task("task_9", "open", { stage_id: "stage_7" }) });
  const notes = [{ author: "gilfoyle", body: "Ranked 30.", ts: later }];
  FakeEventSource.emit("task.updated", {
    payload: task("task_1", "done", { title: "Pull top shorts", updated_at: later, notes }),
  });
  await expect.element(screen.getByTestId("col-done")).toMatchTextContent(/Done 1.*done by gilfoyle.*gilfoyle: Ranked 30\./);
  await expect.element(screen.getByTestId("col-open")).toMatchTextContent(/Open 1.*Rank hooks.*added by richard$/);
  await expect.element(screen.getByTestId("whiteboard-summary")).toHaveTextContent("1 open, 0 claimed, 1 done.");
});

test("add a task by hand from the run page", async () => {
  const posted: unknown[] = [];
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    [TASKS]: () => Response.json([]),
    "POST /api/runs/run_1/stages/stage_0/tasks": async (req) => {
      const body = (await req.json()) as { title: string };
      posted.push(body);
      return Response.json(task("task_5", "open", { title: body.title, added_by: "operator" }), { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("whiteboard-summary")).toHaveTextContent("No tasks yet.");
  await expect.element(screen.getByTestId("col-open")).toHaveTextContent("Open 0Nothing waiting. Add a task to steer the swarm.");
  const add = screen.getByRole("button", { name: "Add task" });
  await expect.element(add).toBeDisabled();
  await screen.getByRole("textbox", { name: "New task" }).fill("Check @datingdecoded this week");
  await add.click();
  await expect.element(screen.getByTestId("col-open")).toMatchTextContent(/Check @datingdecoded this week\s*added by you/);
  expect(posted).toEqual([{ title: "Check @datingdecoded this week", body: "", depends_on: [], added_by: "operator" }]);
  await expect.element(screen.getByRole("textbox", { name: "New task" })).toHaveValue("");

  // The same task arriving on the stream does not duplicate the card.
  FakeEventSource.emit("task.updated", { payload: task("task_5", "open", { title: "Check @datingdecoded this week", added_by: "operator" }) });
  await expect.element(screen.getByTestId("whiteboard-summary")).toHaveTextContent("1 open, 0 claimed, 0 done.");
});

test("a refused task shows why", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(researching),
    [TASKS]: () => Response.json([]),
    "POST /api/runs/run_1/stages/stage_0/tasks": () =>
      Response.json({ detail: "research has ended (done); its whiteboard is read-only" }, { status: 409 }),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await screen.getByRole("textbox", { name: "New task" }).fill("Late idea");
  await screen.getByRole("button", { name: "Add task" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("research has ended (done); its whiteboard is read-only");
});

test("an ended stage's whiteboard is read-only, and stages can be switched", async () => {
  const two = run("running", [
    stage(0, "research", "swarm", "done"),
    stage(1, "pick", "gate", "done"),
    stage(2, "script", "swarm", "running"),
  ]);
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(two),
    [TASKS]: () => Response.json([task("task_1", "done")]),
    "GET /api/runs/run_1/stages/stage_2/tasks": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  const board = screen.getByTestId("whiteboard");
  await expect.element(board.getByRole("heading", { level: 2 })).toHaveTextContent("Whiteboard · script");
  await expect.element(screen.getByRole("button", { name: "Open the whiteboard of script" })).toHaveAttribute("aria-pressed", "true");

  await screen.getByRole("button", { name: "Open the whiteboard of research" }).click();
  await expect.element(board.getByRole("heading", { level: 2 })).toHaveTextContent("Whiteboard · research");
  await expect.element(screen.getByTestId("col-done")).toMatchTextContent(/Task task_1/);
  await expect.element(board).toMatchTextContent(/read-only/);
  expect(board.getByRole("button", { name: "Add task" }).query()).toBeNull();
});

test("a run without swarm stages has no whiteboard", async () => {
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(run("queued", [stage(0, "pick", "gate", "pending")])),
  });
  const { screen } = await mountAt("/worlds/world_1/runs/run_1");
  await expect.element(screen.getByTestId("status-line")).toMatchTextContent(/Queued/);
  expect(screen.getByTestId("whiteboard").query()).toBeNull();
});
