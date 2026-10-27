import { expect, test } from "vitest";
import { render } from "vitest-browser-react";

import type { ArtifactFile } from "../api/events";
import { ArtifactCards, ArtifactView, choosable, Markdown, underReview } from "./Artifacts";

const artifact = (over: Partial<ArtifactFile>): ArtifactFile => ({
  name: "idea-x.md",
  path: "runs/run_1/stage_0/artifacts/idea-x.md",
  type: "idea",
  author: "judge",
  title: "X",
  fields: {},
  body: "",
  run_id: "run_1",
  stage: "stage_0",
  created: "2026-10-26T10:00:00+00:00",
  pinned: false,
  ...over,
});
const chase = artifact({
  name: "idea-chase.md",
  path: "runs/run_1/stage_0/artifacts/idea-chase.md",
  title: "The chase",
  fields: { rank: 2, sources: [] },
  body: "# The chase\n\nA chase in 30 seconds, cut to the beat.",
});
const hooks = artifact({
  name: "idea-hooks.md",
  path: "runs/run_1/stage_0/artifacts/idea-hooks.md",
  title: "Hook first",
  fields: {
    rank: 1,
    hook: "Wait for it",
    why: "Hooks in the first second win.",
    sources: ["runs/run_1/stage_0/notes/trends.md#p3"],
  },
  body: "# Hook first\n\nOpen on the hook.",
});
const notes = artifact({ name: "ideas.md", path: "runs/run_1/stage_0/artifacts/ideas.md", type: "file", title: "ideas.md" });
const verdict = artifact({ name: "verdict-1.md", type: "verdict", title: "Verdict 1: pass" });
const video = artifact({
  name: "short.mp4",
  path: "runs/run_1/stage_2/artifacts/short.mp4.md", // a binary is listed by its sidecar
  type: "video",
  title: "short.mp4",
  stage: "stage_2",
  body: "# short.mp4\n",
});

test("a pick chooses among the typed cards, ranked, each with its hook, reason and source", async () => {
  expect(choosable([notes, chase, verdict, hooks]).map((a) => a.title)).toEqual(["Hook first", "The chase"]);
  expect(choosable([notes, verdict]).map((a) => a.title)).toEqual(["ideas.md"]);

  const screen = await render(<ArtifactCards cards={[hooks, chase]} worldId="world_1" />);
  const cards = screen.getByTestId("pick-card");
  await expect.element(cards.first()).toMatchTextContent(
    /Erlich's #1 of 2.*Hook first.*Wait for it.*Why: Hooks in the first second win\..*From trends\.md, paragraph 3/,
  );
  await expect.element(cards.first().getByRole("link")).toHaveAttribute(
    "href",
    "/api/worlds/world_1/files/runs/run_1/stage_0/notes/trends.md",
  );
  // Without a hook the card summarises the body; without a source it says so.
  await expect.element(cards.nth(1)).toMatchTextContent(/#2 of 2.*The chase.*A chase in 30 seconds.*No source paragraph cited\./);
  await expect.element(cards.nth(1)).not.toMatchTextContent(/Why:/);
  await cards.nth(1).getByRole("radio").click();
  await expect.element(screen.getByRole("radio", { name: /The chase/ })).toBeChecked();
  await expect.element(screen.getByRole("radio", { name: /Hook first/ })).not.toBeChecked();
});

test("the artifact under review is the last stage's, shown as a player, an image or rendered markdown", async () => {
  expect(underReview([hooks, chase, verdict, video]).map((a) => a.name)).toEqual(["short.mp4"]);
  expect(underReview([notes, hooks, chase, verdict]).map((a) => a.name)).toEqual(["ideas.md", "idea-hooks.md", "idea-chase.md"]);

  const image = artifact({ name: "thumb.png", path: "runs/run_1/stage_2/artifacts/thumb.png", type: "file", title: "thumb.png" });
  const script = artifact({
    name: "script.md",
    path: "runs/run_1/stage_1/artifacts/script.md",
    type: "document",
    title: "Script",
    body: "# Five hooks\n\nOpen **cold**, then `cut`.\n\n- One\n- Two\n\nRead [the brief](https://example.com/brief).",
  });
  const gone = artifact({ name: "old.mp4", type: "video", title: "old.mp4", fields: { deleted: "2026-10-27T00:00:00+00:00" } });
  const screen = await render(<ArtifactView artifacts={[video, image, script, gone]} worldId="world_1" />);
  const view = screen.getByRole("region", { name: "Under review" });
  await expect.element(view.getByTestId("player")).toHaveAttribute(
    "src",
    "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/short.mp4",
  );
  await expect.element(view.getByRole("img", { name: "thumb.png" })).toHaveAttribute(
    "src",
    "/api/worlds/world_1/files/runs/run_1/stage_2/artifacts/thumb.png",
  );
  await expect.element(view.getByRole("heading", { level: 3, name: "Five hooks" })).toBeVisible();
  await expect.element(view.getByRole("list")).toMatchTextContent(/One.*Two/);
  await expect.element(view.getByRole("link", { name: "the brief" })).toHaveAttribute("href", "https://example.com/brief");
  await expect.element(view).toMatchTextContent(/old\.mp4 was deleted by retention; its record stays\./);
});

test("markdown renders as elements, never as HTML, and only web links become links", async () => {
  const text = "## Plan\n\n1. First\n2. Second\n\n```yaml\nname: x\n```\n\n<img src=x onerror=alert(1)> and [bad](javascript:alert(1))";
  const screen = await render(<Markdown text={text} />);
  await expect.element(screen.getByRole("heading", { level: 4, name: "Plan" })).toBeVisible();
  await expect.element(screen.getByRole("list")).toMatchTextContent(/First.*Second/);
  await expect.element(screen.getByText("name: x")).toBeVisible();
  await expect.element(screen.getByText(/<img src=x onerror=alert\(1\)> and \[bad\]\(javascript:alert\(1\)\)/)).toBeVisible();
  expect(document.querySelectorAll("img, a").length).toBe(0);
});
