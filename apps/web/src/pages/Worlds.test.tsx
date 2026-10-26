import { afterEach, beforeEach, expect, test, vi } from "vitest";

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

test("lists every world as a card linking to its overview, plus the new-world card", async () => {
  fakeFetch({ "GET /api/health": health, "GET /api/worlds": () => Response.json([video, research]) });
  const { screen } = await mountAt("/");
  await expect.element(screen.getByRole("heading", { name: "Your worlds" })).toBeVisible();
  await expect.element(screen.getByTestId("world-card").nth(1)).toBeVisible();
  const link = screen.getByRole("link", { name: "Video" });
  await expect.element(link).toHaveAttribute("href", "/worlds/world_1");
  await expect.element(screen.getByText("Weekly shorts.")).toBeVisible();
  await expect.element(screen.getByText("No purpose written yet.")).toBeVisible();
  await expect.element(screen.getByRole("form", { name: "New world" })).toBeVisible();
  await expect.element(screen.getByTestId("empty")).not.toBeInTheDocument();
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
