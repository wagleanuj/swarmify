import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = {
  id: "world_1",
  name: "Video",
  purpose: "Weekly shorts.",
  created_at: "2026-10-26T10:00:00+00:00",
};
const research = { id: "world_2", name: "Market research", purpose: "", created_at: video.created_at };
const health = () => Response.json({ version: "0.1.0" });

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists every world as a table row linking to its overview, beside the new-world form", async () => {
  fakeFetch({
    "GET /api/health": health,
    "GET /api/worlds": () => Response.json([video, { ...research, run_cap: 3, monthly_budget_usd: 40 }]),
  });
  const { screen } = await mountAt("/");
  await expect.element(screen.getByRole("heading", { name: "Your worlds", level: 1 })).toBeVisible();
  const rows = screen.getByTestId("world-row");
  await expect.element(rows.nth(1)).toBeVisible();
  const link = screen.getByRole("main").getByRole("link", { name: "Video" });
  await expect.element(link).toHaveAttribute("href", "/worlds/world_1");
  await expect.element(rows.first()).toMatchTextContent(/Video.*2.*No ceiling/);
  await expect.element(rows.nth(1)).toMatchTextContent(/Market research.*3\$40\.00/);
  await expect.element(screen.getByRole("form", { name: "New world" })).toBeVisible();
  await expect.element(screen.getByTestId("empty")).not.toBeInTheDocument();
  await expect.element(screen.getByTestId("eval-worlds")).not.toBeInTheDocument();
});

test("eval worlds fold under their own group and the world in the switcher is marked current", async () => {
  const evals = { id: "world_9", name: "Evals", purpose: "", created_at: video.created_at };
  const trial = { id: "world_8", name: "Eval home-espresso swarm 1", purpose: "", created_at: video.created_at };
  fakeFetch({
    "GET /api/health": health,
    "GET /api/worlds": () => Response.json([video, evals, research, trial]),
    "GET /api/worlds/world_2": () => Response.json(research),
  });
  const { screen, router } = await mountAt("/worlds/world_2");
  await router.navigate({ to: "/" });
  await expect.element(screen.getByRole("main").getByText("2 worlds, plus 2 made by evals", { exact: false })).toBeVisible();
  const group = screen.getByTestId("eval-worlds");
  await expect.element(group.getByText("Eval worlds (2)")).toBeVisible();
  expect(group.getByTestId("world-row").elements()).toHaveLength(2);
  expect(screen.getByTestId("world-row").elements()).toHaveLength(4);
  await expect.element(screen.getByTestId("world-row").nth(1)).toMatchTextContent(/Market research\s*Current/);
  await expect.element(screen.getByTestId("world-row").first()).not.toMatchTextContent(/Current/);
});

test("says what the space is for when there are no worlds", async () => {
  fakeFetch({ "GET /api/health": health, "GET /api/worlds": () => Response.json([]) });
  const { screen } = await mountAt("/");
  await expect.element(screen.getByTestId("empty")).toMatchTextContent(/Nothing here yet/);
  await expect.element(screen.getByRole("button", { name: "Create world" })).toBeEnabled();
});

test("creating a world posts name and purpose and opens the new world", async () => {
  let posted: unknown = null;
  fakeFetch({
    "GET /api/health": health,
    "GET /api/worlds": () => Response.json([]),
    "POST /api/worlds": async (req) => {
      posted = await req.json();
      return Response.json(video, { status: 201 });
    },
    "GET /api/worlds/world_1": () => Response.json(video),
  });
  const { screen, router } = await mountAt("/");
  await screen.getByLabelText("Name").fill("  Video ");
  await screen.getByLabelText("What is this world about?").fill("Weekly shorts.");
  await screen.getByRole("button", { name: "Create world" }).click();
  await expect.element(screen.getByRole("heading", { name: "Video", level: 1 })).toBeVisible();
  expect(posted).toEqual({ name: "Video", purpose: "Weekly shorts." });
  expect(router.state.location.pathname).toBe("/worlds/world_1");
});

test("shows an alert when the API refuses the new world", async () => {
  fakeFetch({
    "GET /api/health": health,
    "GET /api/worlds": () => Response.json([]),
    "POST /api/worlds": () => Response.json({ detail: "nope" }, { status: 422 }),
  });
  const { screen } = await mountAt("/");
  await screen.getByLabelText("Name").fill("x");
  await screen.getByRole("button", { name: "Create world" }).click();
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not create the world/);
});

test("a failing list shows the error, not the empty state", async () => {
  fakeFetch({ "GET /api/health": health, "GET /api/worlds": () => new Response("boom", { status: 500 }) });
  const { screen } = await mountAt("/");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not load worlds/);
  await expect.element(screen.getByTestId("empty")).not.toBeInTheDocument();
});

test("on a phone each world is a stacked row, not a table", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/health": health,
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds": () => Response.json([video, { ...research, run_cap: 3, monthly_budget_usd: 40 }]),
  });
  const { screen } = await mountAt("/");
  const main = screen.getByRole("main");
  await expect.element(main.getByRole("link").filter({ hasText: "Video" })).toBeVisible();
  expect(main.element().querySelector("table")).toBeNull();
  const link = main.getByRole("link").filter({ hasText: "Market research" });
  await expect.element(link).toHaveAttribute("href", "/worlds/world_2");
  await expect.element(link).toMatchTextContent(/Market research.*cap 3 · \$40\.00 a month/);
  await expect.element(main.getByRole("link").filter({ hasText: "Video" })).toMatchTextContent(/cap 2 · no ceiling/);
  expect(link.element().getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("while the worlds load, skeleton rows hold their place", async () => {
  fakeFetch({ "GET /api/health": health, "GET /api/worlds": () => new Promise<Response>(() => undefined) });
  const { screen } = await mountAt("/");
  await expect.element(screen.getByRole("heading", { name: "Your worlds", level: 1 })).toBeVisible();
  const busy = screen.getByRole("main").element().querySelector("[aria-busy='true']");
  expect(busy?.querySelectorAll(".sk")).toHaveLength(3);
  expect(screen.getByText("Loading...").elements()).toHaveLength(0);
});
