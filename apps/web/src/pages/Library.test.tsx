import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { components } from "../api/schema";
import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { plainName } from "./Library";

type Item = components["schemas"]["LibraryItem"];

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const short = { id: "wf_1", world_id: "world_1", name: "Short", current_version: 2, created_at: video.created_at };
const long = { id: "wf_2", world_id: "world_1", name: "Long form", current_version: 1, created_at: video.created_at };
const version = {
  id: "v_2",
  workflow_id: "wf_1",
  number: 2,
  yaml: "",
  created_at: video.created_at,
  model: {
    name: "Short",
    stages: [
      { name: "research", type: "swarm", goal: "", done_when: "" },
      { name: "pick", type: "gate", ask: "" },
      { name: "render", type: "connector", calls: "remotion_render" },
    ],
  },
};

function item(name: string, over: Partial<Item> = {}): Item {
  return {
    path: `runs/run_1/stage_0/artifacts/${name}`,
    name,
    type: "file",
    title: name,
    author: "worker-1",
    created: "2026-10-26T10:00:00+00:00",
    pinned: false,
    body: "",
    fields: {},
    run_id: "run_1",
    run_name: "Short",
    workflow_id: "wf_1",
    workflow_version: 2,
    stage_index: 0,
    stage_name: "research",
    ...over,
  };
}

const hook = item("idea-hook-first.md", { type: "idea", title: "Hook first" });
const ideas = item("ideas.md");
const decision = item("decision.md", {
  path: "runs/run_1/02-pick/decision.md",
  type: "decision",
  stage_index: 1,
  stage_name: "pick",
  created: "2026-10-27T09:00:00+00:00",
});
const brief = item("brief.md", {
  path: "runs/run_2/stage_7/artifacts/brief.md",
  run_id: "run_2",
  run_name: "Long form",
  workflow_id: "wf_2",
  workflow_version: 1,
  stage_name: "brief",
  created: "2026-10-20T09:00:00+00:00",
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists every artifact under type tabs with counts, the tab and search in the address, and filters the rest", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([short, long]),
    "GET /api/worlds/world_1/library": () => Response.json([decision, hook, ideas, brief]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/library");
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Library");
  await expect.element(screen.getByText("4 things the runs in this world made, 0 pinned.")).toBeVisible();
  await expect.element(screen.getByTestId("count")).toHaveTextContent("4 of 4");
  expect(screen.getByTestId("artifact").elements()).toHaveLength(4);
  const row = screen.getByTestId("artifact").first();
  await expect.element(row).toMatchTextContent(/decision\s*decision\.md\s*Short · stage 2 pick · v2/);

  const tabs = screen.getByRole("navigation", { name: "Types" });
  await expect.element(tabs).toMatchTextContent(/All 4\s*decision 1\s*idea 1\s*file 2/);
  await tabs.getByRole("link", { name: /^idea/ }).click();
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ type: "idea" }));
  await expect.element(tabs.getByRole("link", { name: /^idea/ })).toHaveAttribute("aria-current", "page");
  await expect.element(tabs.getByRole("link", { name: /^All/ })).not.toHaveAttribute("aria-current");
  await expect.element(screen.getByTestId("count")).toHaveTextContent("1 of 4");
  await expect.element(screen.getByTestId("library")).toMatchTextContent(/^idea\s*Hook first/);
  await tabs.getByRole("link", { name: /^All/ }).click();
  await expect.element(tabs.getByRole("link", { name: /^All/ })).toHaveAttribute("aria-current", "page");
  await expect.element(screen.getByTestId("count")).toHaveTextContent("4 of 4");

  await screen.getByRole("searchbox", { name: "Search the library" }).fill("brief");
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ q: "brief" }));
  await expect.element(screen.getByTestId("library")).toMatchTextContent(/^file\s*brief\.md/);
  await screen.getByRole("searchbox", { name: "Search the library" }).fill("");

  const filters = screen.getByRole("form", { name: "Filters" });
  await filters.getByLabelText(/^Workflow/).selectOptions("Long form");
  await expect.element(screen.getByTestId("library")).toMatchTextContent(/^file\s*brief\.md/);
  await filters.getByRole("button", { name: "Clear filters" }).click();

  await filters.getByLabelText(/^Stage/).selectOptions("pick");
  await expect.element(screen.getByTestId("library")).toMatchTextContent(/^decision/);
  await filters.getByRole("button", { name: "Clear filters" }).click();

  await filters.getByLabelText(/^Run/).selectOptions("Long form run_2");
  await expect.element(screen.getByTestId("count")).toHaveTextContent("1 of 4");
  await filters.getByRole("button", { name: "Clear filters" }).click();

  await filters.getByLabelText(/^From/).fill("2026-10-26");
  await filters.getByLabelText(/^To/).fill("2026-10-26");
  await expect.element(screen.getByTestId("count")).toHaveTextContent("2 of 4");
  await filters.getByLabelText(/^To/).fill("2026-10-25");
  await expect.element(screen.getByText("No artifact matches these filters.")).toBeVisible();
});

test("the address names the item in the preview, and pinning keeps it there with a stamp and lists it first", async () => {
  const pins: unknown[] = [];
  const notes = item("ideas.md", { body: "# Ideas\n\nOpen on the **hook**." });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([short]),
    "GET /api/worlds/world_1/library": () => Response.json([notes, hook]),
    "POST /api/worlds/world_1/pins": async (req) => {
      const body = (await req.json()) as { path: string; pinned: boolean };
      pins.push(body);
      return Response.json({ ...hook, pinned: body.pinned });
    },
  });
  const { screen, router } = await mountAt(`/worlds/world_1/library?item=${encodeURIComponent(notes.path)}`);
  const preview = screen.getByTestId("preview");
  await expect.element(preview.getByRole("heading", { level: 2 })).toHaveTextContent("ideas.md");
  await expect.element(preview).toMatchTextContent(/Open on the hook\./);
  await expect.element(screen.getByTestId("artifact").first()).toHaveAttribute("aria-current", "page");

  await screen.getByRole("link", { name: /Hook first/ }).click();
  await vi.waitFor(() => expect(router.state.location.search).toEqual({ item: hook.path }));
  await expect.element(preview.getByRole("heading", { level: 2 })).toHaveTextContent("Hook first");
  await expect.element(preview.getByRole("link", { name: "Short" })).toHaveAttribute("href", "/worlds/world_1/runs/run_1");

  await preview.getByRole("button", { name: "Pin Hook first" }).click();
  await expect.element(preview.getByRole("status")).toHaveTextContent("Pinned. Later stages read it.");
  expect(router.state.location.search).toEqual({ item: hook.path });
  const first = screen.getByTestId("artifact").first();
  await expect.element(first).toHaveAttribute("data-pinned", "true");
  await expect.element(first).toMatchTextContent(/Pinned · Hook first/);
  await preview.getByRole("button", { name: "Unpin Hook first" }).click();
  await expect.element(preview.getByRole("status")).toHaveTextContent("Unpinned.");
  expect(pins).toEqual([
    { path: hook.path, pinned: true },
    { path: hook.path, pinned: false },
  ]);
});

test("start a run from here is a menu naming the stage each new run starts at, and opens the run", async () => {
  let promoted: unknown = null;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([long, short]),
    "GET /api/worlds/world_1/library": () => Response.json([ideas]),
    "GET /api/workflows/wf_1/versions/2": () => Response.json(version),
    "GET /api/workflows/wf_2/versions/1": () =>
      Response.json({ ...version, model: { name: "Long form", stages: [{ name: "brief", type: "swarm", goal: "", done_when: "" }, { name: "edit", type: "swarm", goal: "", done_when: "" }] } }),
    "POST /api/workflows/wf_1/promote": async (req) => {
      promoted = await req.json();
      return Response.json({ id: "run_9" }, { status: 201 });
    },
  });
  const { screen, router } = await mountAt(`/worlds/world_1/library?item=${encodeURIComponent(ideas.path)}`);
  await screen.getByRole("button", { name: "Start a run from here" }).click();
  const menu = screen.getByRole("menu", { name: "Start a run from here" });
  // Its own workflow first, after the stage that made it; then the others from their second stage.
  await expect.element(menu).toMatchTextContent(
    /^pick\s*Opens a new run of Short at pick, using this file\s*render\s*Opens a new run of Short at render.*edit\s*Opens a new run of Long form at edit/,
  );
  await menu.getByRole("menuitem", { name: /^render/ }).click();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/worlds/world_1/runs/run_9"));
  expect(promoted).toEqual({ artifact: ideas.path, stage: 2 });
});

test("a refused start shows the API's reason", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([short]),
    "GET /api/worlds/world_1/library": () => Response.json([ideas]),
    "GET /api/workflows/wf_1/versions/2": () => Response.json(version),
    "POST /api/workflows/wf_1/promote": () => Response.json({ detail: "artifact not found" }, { status: 404 }),
  });
  const { screen } = await mountAt(`/worlds/world_1/library?item=${encodeURIComponent(ideas.path)}`);
  await screen.getByRole("button", { name: "Start a run from here" }).click();
  await screen.getByRole("menuitem", { name: /^pick/ }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("artifact not found");
});

test("with nothing made yet it says what lands here and reloads when an artifact is created", async () => {
  let listed: Item[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
    "GET /api/worlds/world_1/library": () => Response.json(listed),
  });
  const { screen } = await mountAt("/worlds/world_1/library");
  await expect.element(screen.getByTestId("no-artifacts")).toMatchTextContent(/Nothing yet/);
  await expect.element(screen.getByRole("link", { name: "See runs" })).toHaveAttribute("href", "/worlds/world_1/runs");
  listed = [hook];
  FakeEventSource.emit("artifact.created", { payload: { type: "idea", path: hook.path, author: "judge", stage_id: "stage_0" } });
  await expect.element(screen.getByTestId("library")).toMatchTextContent(/Hook first/);
});

test("a load failure is an alert and the overview links here", async () => {
  fakeFetch({ "GET /api/worlds/world_1/library": () => new Response("boom", { status: 500 }) });
  const { screen } = await mountAt("/worlds/world_1/library");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not load the library/);

  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([]),
  });
  const overview = await mountAt("/worlds/world_1");
  await expect
    .element(overview.screen.getByRole("link", { name: "Open the library" }))
    .toHaveAttribute("href", "/worlds/world_1/library");
});

test("a note or artifact becomes a skill draft the operator edits and saves as a version", async () => {
  const bodies: unknown[] = [];
  const note = item("trends.md", {
    path: "runs/run_1/stage_0/notes/trends.md",
    type: "note",
    title: "Trends: hooks win",
    body: "Open on the hook.\n\nKeep the first second moving.",
  });
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([short]),
    "GET /api/worlds/world_1/library": () => Response.json([note]),
    "POST /api/worlds/world_1/skills": async (req) => {
      bodies.push(await req.json());
      return Response.json(
        { name: "hooks-win", description: "Trends: hooks win", source: "world", path: "x", content: "", version: 1, run_id: "run_1", created_at: video.created_at },
        { status: 201 },
      );
    },
  });
  const { screen } = await mountAt(`/worlds/world_1/library?item=${encodeURIComponent(note.path)}`);
  await screen.getByRole("button", { name: "Make a skill from Trends: hooks win" }).click();
  const form = screen.getByRole("form", { name: "Make a skill from Trends: hooks win" });
  await expect.element(form.getByLabelText("Skill name")).toHaveValue("trends-hooks-win");
  await expect.element(form.getByRole("textbox", { name: "SKILL.md" })).toHaveValue(
    "---\nname: trends-hooks-win\ndescription: Trends: hooks win\n---\n\nOpen on the hook.\n\nKeep the first second moving.\n",
  );
  await form.getByLabelText("Skill name").fill("hooks-win");
  await expect.element(form.getByRole("textbox", { name: "SKILL.md" })).toHaveValue(
    "---\nname: hooks-win\ndescription: Trends: hooks win\n---\n\nOpen on the hook.\n\nKeep the first second moving.\n",
  );
  await form.getByRole("button", { name: "Save skill" }).click();
  await vi.waitFor(() => expect(bodies).toHaveLength(1));
  expect(bodies[0]).toMatchObject({ name: "hooks-win", run_id: "run_1", note: "Promoted from runs/run_1/stage_0/notes/trends.md" });
  expect((bodies[0] as { content: string }).content).toMatch(/^---\nname: hooks-win\ndescription: Trends: hooks win\n---/);
  await expect.element(screen.getByRole("status")).toMatchTextContent(/Saved hooks-win as version 1/);
  await expect.element(screen.getByRole("link", { name: "See the skills" })).toHaveAttribute("href", "/worlds/world_1/cast");
});

test("a skill the API refuses shows why", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([short]),
    "GET /api/worlds/world_1/library": () => Response.json([ideas]),
    "POST /api/worlds/world_1/skills": () =>
      Response.json({ detail: "the SKILL.md frontmatter needs a description" }, { status: 422 }),
  });
  const { screen } = await mountAt("/worlds/world_1/library");
  await screen.getByRole("link", { name: /ideas\.md/ }).click();
  await screen.getByRole("button", { name: "Make a skill from ideas.md" }).click();
  await screen.getByRole("button", { name: "Save skill" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("the SKILL.md frontmatter needs a description");
});

test("assets are listed with links and uploaded through the API", async () => {
  const bed = { path: "assets/bed-calm.mp3", name: "bed-calm.mp3", bytes: 2_400_000 };
  const uploads: [string, string][] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/workflows": () => Response.json([short]),
    "GET /api/worlds/world_1/library": () => Response.json([]),
    "GET /api/worlds/world_1/assets": () => Response.json(uploads.length ? [bed, { ...bed, path: "assets/whoosh.wav", name: "whoosh.wav", bytes: 4 }] : [bed]),
    "PUT /api/worlds/world_1/assets/whoosh.wav": async (req) => {
      uploads.push([new URL(req.url).pathname, await req.text()]);
      return Response.json({ path: "assets/whoosh.wav", name: "whoosh.wav", bytes: 4 }, { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/library");
  const assets = screen.getByRole("region", { name: "Assets" });
  await expect
    .element(assets.getByRole("link", { name: "bed-calm.mp3" }))
    .toHaveAttribute("href", "/api/worlds/world_1/files/assets/bed-calm.mp3");
  await expect.element(assets).toMatchTextContent(/2\.4 MB/);
  await assets.getByLabelText("Upload files").upload(new File(["RIFF"], "whoosh.wav"));
  await expect.element(assets.getByRole("link", { name: "whoosh.wav" })).toBeVisible();
  expect(uploads).toEqual([["/api/worlds/world_1/assets/whoosh.wav", "RIFF"]]);
});

test("an uploaded file gets a name the API takes", () => {
  expect(plainName("Calm Bed (loop).mp3")).toBe("Calm-Bed-loop-.mp3");
  expect(plainName(".hidden.wav")).toBe("hidden.wav");
  expect(plainName("logo.png")).toBe("logo.png");
});
