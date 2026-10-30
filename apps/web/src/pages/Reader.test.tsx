import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import type { ArtifactFile } from "../api/events";
import type { components } from "../api/schema";
import { FakeEventSource, fakeFetch, run, stage } from "../test/fakes";
import { mountAt } from "../test/mount";

type Item = components["schemas"]["LibraryItem"];

const line = `Narrator: ${"the swarm keeps writing one long sentence without a break ".repeat(5)}`.slice(0, 300);
const BODY = [
  "# The chase",
  line,
  '```json\n{"voice": "Calm and dry, like a nature documentary.", "beats": [{"at": 0, "say": "' + "x".repeat(200) + '"}]}\n```',
  "| scene | line | seconds | camera | music | notes |\n| --- | --- | --- | --- | --- | --- |\n| 1 | " +
    "Open on the empty street at dawn ".repeat(4) +
    "| 4 | wide | calm bed | none |",
  "See https://example.com/a/very/long/path/that/never/breaks/" + "segment/".repeat(20),
].join("\n\n");

const script: Item = {
  path: "runs/run_1/stage_2/artifacts/script.md",
  name: "script.md",
  type: "script",
  title: "The chase, script",
  author: "worker-1",
  created: "2026-10-26T10:00:00+00:00",
  pinned: false,
  body: BODY,
  fields: { type: "script" },
  run_id: "run_1",
  run_name: "Short",
  workflow_id: "wf_1",
  workflow_version: 2,
  stage_index: 2,
  stage_name: "script",
};

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const at = `/worlds/world_1/read?${new URLSearchParams({ path: script.path })}`;

test("on a phone the reader shows a script wrapped, its code and table scrolling inside themselves", async () => {
  await page.viewport(390, 844);
  fakeFetch({ "GET /api/worlds/world_1/library": () => Response.json([script]) });
  const { screen } = await mountAt(at);
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("The chase, script");
  await expect.element(screen.getByText("script", { exact: true }).first()).toBeVisible();
  await expect.element(screen.getByRole("link", { name: "Short" })).toHaveAttribute("href", "/worlds/world_1/runs/run_1");
  await expect.element(screen.getByRole("link", { name: "step 3, script" })).toHaveAttribute("href", "/worlds/world_1/runs/run_1?stage=stage_2");
  await expect.element(screen.getByRole("link", { name: "Raw file" })).toHaveAttribute("href", "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/script.md");
  await expect.element(screen.getByTestId("typed-text")).toMatchTextContent(/voice\s*Calm and dry/);
  const doc = screen.getByTestId("document");
  await expect.element(doc.getByRole("table")).toBeVisible();
  for (const el of [doc.element().querySelector("pre")!, doc.element().querySelector(".scroll-x")!]) {
    expect(el.scrollWidth).toBeGreaterThan(el.clientWidth); // scrolls inside itself
    expect(el.getBoundingClientRect().right).toBeLessThanOrEqual(390);
  }
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("a note the library does not list is read from its file, frontmatter dropped", async () => {
  const path = "runs/run_1/stage_0/notes/trends.md";
  fakeFetch({
    "GET /api/worlds/world_1/library": () => Response.json([]),
    [`GET /api/worlds/world_1/files/${path}`]: () => new Response("---\nauthor: worker-1\n---\n# Trends\n\nHooks in the first second win."),
  });
  const { screen } = await mountAt(`/worlds/world_1/read?${new URLSearchParams({ path })}`);
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("trends.md");
  await expect.element(screen.getByTestId("document")).toMatchTextContent(/^Trends\s*Hooks in the first second win\.$/);
  await expect.element(screen.getByRole("link", { name: "the run" })).toHaveAttribute("href", "/worlds/world_1/runs/run_1");
});

test("Open on a run's document goes to the reader; a video still opens raw", async () => {
  const made = (name: string, over: Partial<ArtifactFile> = {}): ArtifactFile => ({
    ...script,
    path: `runs/run_1/stage_0/artifacts/${name}`,
    name,
    type: "file",
    title: name,
    stage: "stage_0",
    ...over,
  });
  const done = run("done", [stage(0, "research", "swarm", "done", { started_at: "2026-10-26T10:00:00Z", ended_at: "2026-10-26T10:02:00Z" })]);
  const notes = made("notes.md", { body: "# Notes" });
  fakeFetch({
    "GET /api/runs/run_1": () => Response.json(done),
    "GET /api/runs/run_1/artifacts": () => Response.json([notes, made("clip.mp4")]),
    "GET /api/worlds/world_1/library": () => Response.json([{ ...script, ...notes }]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/runs/run_1?stage=stage_0");
  await screen.getByText("All files (2)").click();
  await expect.element(screen.getByTestId("all-files").getByRole("link", { name: "clip.mp4" })).toHaveAttribute("href", "/api/worlds/world_1/files/runs/run_1/stage_0/artifacts/clip.mp4");
  await screen.getByTestId("all-files").getByRole("link", { name: "notes.md" }).click();
  await vi.waitFor(() => expect(router.state.location.pathname).toBe("/worlds/world_1/read"));
  expect(router.state.location.search).toEqual({ path: notes.path });
  await expect.element(screen.getByRole("heading", { level: 1 })).toHaveTextContent("notes.md");
});

test("on a phone a relative image loads from the files route, fits the width, and an escaping one shows its alt", async () => {
  await page.viewport(390, 844);
  const png = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="), (c) => c.charCodeAt(0));
  const body = "# Frames\n\n![The first frame](../build/frame.png)\n\n![Escaped](../../../../../secret.png) and ![Root](/etc/passwd.png)";
  fakeFetch({ "GET /api/worlds/world_1/library": () => Response.json([{ ...script, body }]) });
  const { screen } = await mountAt(at);
  const img = screen.getByRole("img", { name: "The first frame" });
  await expect.element(img).toHaveAttribute("src", "/api/worlds/world_1/files/runs/run_1/stage_2/build/frame.png");
  const el = img.element() as HTMLImageElement;
  el.src = URL.createObjectURL(new Blob([png], { type: "image/png" })); // a real image, stretched to show it fits
  el.style.width = "2000px";
  await vi.waitFor(() => expect(el.complete && el.naturalWidth).toBe(1));
  expect(el.getBoundingClientRect().right).toBeLessThanOrEqual(390);
  const doc = screen.getByTestId("document");
  await expect.element(doc).toMatchTextContent(/Escaped and Root/);
  expect(doc.element().querySelectorAll("img").length).toBe(1);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});
