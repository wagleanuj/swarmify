import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch, run } from "../test/fakes";
import { mountAt } from "../test/mount";

const ts = "2026-10-26T10:00:00+00:00";
const workflows = [
  { id: "wf_1", world_id: "world_1", name: "Weekly short", current_version: 3, created_at: ts },
  { id: "wf_2", world_id: "world_1", name: "Daily digest", current_version: 1, created_at: ts },
];
const runs = [
  run("done", [], { id: "run_1", workflow_id: "wf_1", spent_usd: 4 }),
  run("done", [], { id: "run_2", workflow_id: "wf_1", spent_usd: 2.5 }),
  run("done", [], { id: "run_3", workflow_id: null, spent_usd: 9 }),
];

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the sidebar's Workflows lists each with its version, runs and average cost, and starts a run", async () => {
  let started: unknown = null;
  fakeFetch({
    "GET /api/worlds": () => Response.json([{ id: "world_1", name: "Video", purpose: "", created_at: ts }]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/workflows": () => Response.json(workflows),
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
    "POST /api/workflows/wf_1/runs": async (req) => {
      started = await req.json();
      return Response.json(run("queued", [], { id: "run_9" }), { status: 201 });
    },
    "GET /api/runs/run_9": () => Response.json(run("queued", [], { id: "run_9" })),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows");
  const nav = screen.getByRole("navigation", { name: "Main" });
  await expect.element(nav.getByRole("link", { name: "Workflows" })).toHaveAttribute("href", "/worlds/world_1/workflows");
  await expect.element(nav.getByRole("link", { name: "Workflows" })).toHaveAttribute("aria-current", "page");
  await expect.element(screen.getByRole("heading", { name: "Workflows", level: 1 })).toBeVisible();
  const rows = screen.getByTestId("workflow");
  await expect.element(rows.first()).toMatchTextContent(/Weekly short.*v3.*done.*2.*\$3\.25/);
  await expect.element(rows.nth(1)).toMatchTextContent(/Daily digest.*v1.*-.*0.*-/);
  // One outlined play glyph per row: the only filled control is New workflow.
  expect(rows.first().getByRole("button", { name: "Start a run of Weekly short" }).element().classList).not.toContain("primary");
  expect(document.querySelectorAll("main .primary")).toHaveLength(1);
  await expect.element(rows.first().getByRole("link")).toHaveAttribute("href", "/worlds/world_1/workflows/wf_1");
  await expect
    .element(screen.getByRole("link", { name: "New workflow" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/new");

  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await expect.poll(() => router.state.location.pathname).toBe("/worlds/world_1/runs/run_9");
  expect(started).toEqual({ baseline: false });
  // Not the world's first run: no first-run moment.
  expect(document.querySelector(".toasts")?.textContent ?? "").not.toMatch(/first run/);
});

test("a world's first run is a success moment: a notice whose check pops", async () => {
  fakeFetch({
    "GET /api/worlds": () => Response.json([{ id: "world_1", name: "Video", purpose: "", created_at: ts }]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/workflows": () => Response.json(workflows),
    "GET /api/worlds/world_1/runs": () => Response.json([]),
    "POST /api/workflows/wf_1/runs": () => Response.json(run("queued", [], { id: "run_9" }), { status: 201 }),
    "GET /api/runs/run_9": () => Response.json(run("queued", [], { id: "run_9" })),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows");
  await screen.getByRole("button", { name: "Start a run of Weekly short" }).click();
  await expect.poll(() => router.state.location.pathname).toBe("/worlds/world_1/runs/run_9");
  const said = screen.getByText(/Your first run is on its way\./);
  await expect.element(said).toBeVisible();
  expect(said.element().parentElement!.querySelector(".i-pop")).not.toBeNull();
});

test("on a phone each workflow is one row: full name, version, last run and cost, a play glyph, New workflow in the header", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/worlds": () => Response.json([{ id: "world_1", name: "Video", purpose: "", created_at: ts }]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/workflows": () =>
      Response.json([{ ...workflows[0], name: "A very long workflow name that runs on (with a note)" }, workflows[1]]),
    "GET /api/worlds/world_1/runs": () =>
      Response.json([
        run("waiting_gate", [], { id: "run_1", workflow_id: "wf_1", spent_usd: 4, budget_usd: 15, created_at: "2026-10-27T10:00:00+00:00" }),
        ...runs.slice(1),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/workflows");
  const rows = screen.getByTestId("workflow-row");
  await expect.element(rows.first()).toBeVisible();
  expect(screen.getByTestId("workflows").elements()).toHaveLength(0);
  await expect.element(rows.first()).toMatchTextContent(/^A very long workflow name that runs on \(with a note\)/);
  await expect.element(rows.first()).toMatchTextContent(/v3 · waiting for you · \$3\.25 a run/);
  await expect.element(rows.nth(1)).toMatchTextContent(/Daily digest\s*v1 · no runs yet/);
  const start = rows.first().getByRole("button", { name: "Start a run of A very long workflow name that runs on (with a note)" });
  const box = start.element().getBoundingClientRect();
  expect(box.height).toBeGreaterThanOrEqual(44);
  expect(box.width).toBeLessThan(60);
  await expect
    .element(rows.first().getByRole("link", { name: "Edit A very long workflow name that runs on (with a note)" }))
    .toHaveAttribute("href", "/worlds/world_1/workflows/wf_1");
  const create = screen.getByRole("link", { name: "New workflow" });
  expect(create.element().getBoundingClientRect().bottom).toBeLessThan(rows.first().element().getBoundingClientRect().top);
  expect(document.querySelectorAll("main .primary")).toHaveLength(1);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("the rows show before the slow check answers, then a failing workflow says so", async () => {
  let answer = (_: Response) => {};
  const checked = new Promise<Response>((resolve) => (answer = resolve));
  fakeFetch({
    "GET /api/worlds": () => Response.json([{ id: "world_1", name: "Video", purpose: "", created_at: ts }]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/workflows": (req) =>
      new URL(req.url).searchParams.get("check") === "false" ? Response.json(workflows) : checked,
    "GET /api/worlds/world_1/runs": () => Response.json(runs),
  });
  const { screen } = await mountAt("/worlds/world_1/workflows");
  const rows = screen.getByTestId("workflow");
  await expect.element(rows.first()).toMatchTextContent(/^Weekly short/);
  await expect.element(rows.first()).not.toMatchTextContent(/Fails today's check/);

  const violations = [{ rule: "unknown_connector", message: "connector 'gone' is not installed" }];
  answer(Response.json([{ ...workflows[0], violations }, workflows[1]]));
  await expect.element(rows.first()).toMatchTextContent(/Fails today's check: connector 'gone' is not installed\./);
});

test("on a phone, while the workflows load, four rows wait as skeletons, then the rows fade up", async () => {
  await page.viewport(390, 844);
  let release = () => {};
  const held = new Promise<void>((r) => (release = r));
  fakeFetch({
    "GET /api/worlds": () => Response.json([{ id: "world_1", name: "Video", purpose: "", created_at: ts }]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/workflows": async () => {
      await held;
      return Response.json(workflows);
    },
    "GET /api/worlds/world_1/runs": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/workflows");
  await expect.element(screen.getByRole("heading", { name: "Workflows", level: 1 })).toBeVisible();
  await expect.poll(() => document.querySelector("main [aria-busy='true']")?.querySelectorAll(".sk").length).toBe(4);
  expect(screen.getByText("Loading...").elements()).toHaveLength(0);

  release();
  await expect.element(screen.getByTestId("workflow-row").first()).toBeVisible();
  expect(document.querySelector("main [aria-busy='true']")).toBeNull();
  expect(screen.getByTestId("workflow-row").first().element().parentElement?.classList).toContain("stagger");
});
