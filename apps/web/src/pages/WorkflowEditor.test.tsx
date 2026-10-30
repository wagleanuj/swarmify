import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { STARTER } from "./WorkflowEditor";

const video = {
  id: "world_1",
  name: "Video",
  purpose: "Weekly shorts.",
  created_at: "2026-10-26T10:00:00+00:00",
};
const model = {
  name: "Weekly short",
  stages: [
    { type: "swarm", name: "research", goal: "g", done_when: "d" },
    { type: "gate", name: "pick", ask: "a", kind: "pick" },
  ],
};
const v1 = {
  id: "wfv_1",
  workflow_id: "wf_1",
  number: 1,
  yaml: STARTER,
  created_at: video.created_at,
  model,
};
const publishProblem = {
  rule: "publish_without_gate",
  message: "publish connector 'youtube' must come right after a gate",
  stage: "youtube",
};
const yamlProblem = { rule: "yaml", message: "not valid YAML at line 2: bad", stage: null };

/** A check endpoint that answers from the YAML it receives: broken text gets violations. */
async function check(req: Request) {
  const { yaml } = (await req.json()) as { yaml: string };
  if (yaml.includes("youtube")) return Response.json({ violations: [publishProblem], model });
  if (yaml.includes("[[")) return Response.json({ violations: [yamlProblem], model: null });
  return Response.json({ violations: [], model: { ...model, name: yaml.split("\n")[0]?.slice(6) } });
}

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1280, 800);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a new workflow opens on Stages: each step with what it does, needs and makes, and a quiet checks line", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": check,
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new");
  await expect.element(screen.getByRole("heading", { name: "Weekly short", level: 1 })).toBeVisible();
  await expect.element(screen.getByRole("tab", { name: "Stages" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "Scorecard" }).elements()).toHaveLength(0);
  expect(screen.getByLabelText("Workflow YAML").elements()).toHaveLength(0);
  const rows = screen.getByTestId("stage-rows").getByRole("button");
  await expect.element(rows.first()).toMatchTextContent(/swarm.*research.*Find 5 short-form.*makes list\[idea\]/);
  await expect.element(rows.nth(1)).toMatchTextContent(/gate.*pick.*Pick one idea.*needs list\[idea\].*makes list\[idea\] \(one picked\)/);
  await expect.element(rows.first()).toHaveAttribute("aria-current", "true");
  await expect.element(screen.getByTestId("stage-contract")).toMatchTextContent(/Needs.*nothing, it starts the run.*Makes.*list\[idea\]/);
  await expect.element(screen.getByTestId("problems")).toHaveTextContent("Checks pass: every step gets the type it needs.");
  await expect.element(screen.getByText("Version 1 is created when you save.")).toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Save as version 1" })).toBeEnabled();

  await screen.getByRole("tab", { name: "YAML" }).click();
  await expect.element(screen.getByLabelText("Workflow YAML")).toHaveValue(STARTER);
});

test("violations appear inline as you type and block saving until fixed", async () => {
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": check,
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new?tab=yaml");
  const editor = screen.getByLabelText("Workflow YAML");
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/Checks pass/);

  await editor.fill(`${STARTER}  - connector: youtube\n`);
  await expect.element(screen.getByRole("heading", { name: "1 problem to fix before saving" })).toBeVisible();
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(
    /youtube: publish connector 'youtube' must come right after a gate/,
  );
  await expect.element(screen.getByRole("button", { name: "Save as version 1" })).toBeDisabled();

  await editor.fill("name: [[\n");
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/not valid YAML at line 2/);
  await screen.getByRole("tab", { name: "Stages" }).click();
  await expect.element(screen.getByTestId("stages-unavailable")).toBeVisible();
  await screen.getByRole("tab", { name: "YAML" }).click();

  await editor.fill(STARTER);
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/Checks pass/);
  await expect.element(screen.getByRole("button", { name: "Save as version 1" })).toBeEnabled();
  expect(calls.filter((c) => c.startsWith("POST /api/worlds/world_1/workflows/check")).length).toBeGreaterThan(2);
});

test("saving a new workflow posts the YAML and stays in its editor with the version stamp", async () => {
  let posted: unknown = null;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": check,
    "POST /api/worlds/world_1/workflows": async (req) => {
      posted = await req.json();
      return Response.json(v1, { status: 201 });
    },
    "GET /api/workflows/wf_1/versions": () => Response.json([v1]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/new");
  await screen.getByRole("button", { name: "Save as version 1" }).click();
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v1/);
  expect(posted).toEqual({ yaml: STARTER });
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_1");
  await expect.element(screen.getByRole("button", { name: "Save as version 2" })).toBeDisabled();
  await expect.element(screen.getByTestId("check-status")).toHaveTextContent(
    "Saved as version 1; runs already started keep their version.",
  );
  await expect.element(screen.getByRole("tab", { name: "Stages" })).toHaveAttribute("aria-selected", "true");
});

test("an existing workflow loads its latest version, saves a new one and can open old ones", async () => {
  const v2 = { ...v1, id: "wfv_2", number: 2, yaml: STARTER.replace("Weekly short", "Daily short") };
  const versions = [v1, v2];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": check,
    "GET /api/workflows/wf_1/versions": () => Response.json(versions),
    "POST /api/workflows/wf_1/versions": async (req) => {
      const { yaml } = (await req.json()) as { yaml: string };
      const v3 = { ...v1, id: "wfv_3", number: 3, yaml };
      versions.push(v3);
      return Response.json(v3, { status: 201 });
    },
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/wf_1");
  await expect.element(screen.getByRole("heading", { name: "Daily short v2", level: 1 })).toBeVisible();
  await screen.getByRole("tab", { name: "YAML" }).click();
  expect(router.state.location.search).toEqual({ tab: "yaml" });
  const editor = screen.getByLabelText("Workflow YAML");
  await expect.element(editor).toHaveValue(v2.yaml);
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v2.*open.*v1.*Open v1/);
  await expect.element(screen.getByRole("button", { name: "Save as version 3" })).toBeDisabled();

  await screen.getByRole("button", { name: "Open v1" }).click();
  await expect.element(editor).toHaveValue(STARTER);
  await expect.element(screen.getByTestId("check-status")).toHaveTextContent("Unsaved changes.");
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v2.*Open v2.*v1.*open/);
  await editor.fill(`${STARTER}# v3\n`);
  await screen.getByRole("button", { name: "Save as version 3" }).click();
  await expect.element(screen.getByTestId("check-status")).toHaveTextContent(
    "Saved as version 3; runs already started keep their version.",
  );
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_1");
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v3.*open.*v2.*Open v2.*v1.*Open v1/);
  await expect.element(screen.getByRole("button", { name: "Save as version 4" })).toBeDisabled();
});

test("a save the API refuses shows its violations inline", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": () => Response.json({ violations: [], model }),
    "POST /api/worlds/world_1/workflows": () =>
      Response.json({ violations: [publishProblem], model: null }, { status: 422 }),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/new");
  await screen.getByRole("button", { name: "Save as version 1" }).click();
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/must come right after a gate/);
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/new");
});

test("a workflow that no longer exists is an alert", async () => {
  fakeFetch({ "GET /api/worlds/world_1": () => Response.json(video) });
  const { screen } = await mountAt("/worlds/world_1/workflows/wf_gone");
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/Could not load this workflow/);
});

const planned = `name: Weekly short
schedule: 0 8 * * 1
stages:
  - swarm: research
    goal: Find 5 ideas.
    done_when: 5 ideas ranked
  - gate: pick
    ask: Pick one.
    kind: pick
`;

test("the stage list edits the same text the YAML view shows: fields, order, add, remove", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds": () => Response.json([video]),
    "POST /api/worlds/world_1/workflows/check": check,
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new");
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/Checks pass/);
  await expect.element(screen.getByTestId("stage-rows")).toMatchTextContent(/swarm.*gate/);
  await expect.element(screen.getByLabelText("Goal of research")).toHaveValue(
    "Find 5 short-form video ideas trending this week. Check the graph for what we already published.",
  );
  await expect.element(screen.getByLabelText("Tools of research")).toHaveValue("web_search, graph");

  await screen.getByLabelText("Done when of research").fill("3 ideas ranked");
  await screen.getByLabelText("Tools of research").fill("web_search");
  // ADR 0020: a swarm stage runs one agent unless it says more.
  await expect.element(screen.getByLabelText("Agents of research")).toHaveAttribute("placeholder", "1");
  await screen.getByLabelText("Agents of research").fill("3");
  await screen.getByTestId("stage-rows").getByRole("button", { name: /pick/ }).click();
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/pick.*gate/);
  await screen.getByLabelText("Move pick up").click();
  await expect.element(screen.getByTestId("stage-rows")).toMatchTextContent(/gate.*swarm/);
  // the selection follows the moved stage
  await expect.element(screen.getByTestId("stage-rows").getByRole("button").first()).toHaveAttribute("aria-current", "true");
  await screen.getByLabelText("Remove pick").click();
  await screen.getByLabelText("Stage type to add").selectOptions("connector");
  await screen.getByRole("button", { name: "Add stage" }).click();
  await expect.element(screen.getByTestId("stage-panel")).toMatchTextContent(/render.*connector/);

  await screen.getByRole("tab", { name: "YAML" }).click();
  const yaml = screen.getByLabelText("Workflow YAML");
  await expect.element(yaml).toHaveValue(
    `name: Weekly short
budget_usd: 5
stages:
  - swarm: research
    goal: Find 5 short-form video ideas trending this week. Check the graph for what we already published.
    done_when: 3 ideas ranked
    tools: [ web_search ]
    output_type: list[idea]
    agents: 3
  - connector: render
    calls: remotion_render
`,
  );

  // and the other way round: YAML typed by hand lands in the list
  await yaml.fill(planned);
  await screen.getByRole("tab", { name: "Stages" }).click();
  await screen.getByTestId("stage-rows").getByRole("button", { name: /pick/ }).click();
  await expect.element(screen.getByLabelText("Ask of pick")).toHaveValue("Pick one.");
  await expect.element(screen.getByLabelText("Schedule (cron, UTC)")).toHaveValue("0 8 * * 1");
});

test("a schedule shows its next run from the check, in the list and the aside", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds": () => Response.json([video]),
    "POST /api/worlds/world_1/workflows/check": async (req) => {
      const { yaml } = (await req.json()) as { yaml: string };
      const timed = yaml.includes("schedule:");
      return Response.json({
        violations: [],
        model: { ...model, schedule: timed ? "0 8 * * 1" : null },
        next_run_at: timed ? "2026-11-02T08:00:00+00:00" : null,
      });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new");
  await expect.element(screen.getByTestId("schedule")).toMatchTextContent(/None\. Add a cron schedule/);
  await screen.getByRole("tab", { name: "Stages" }).click();
  await expect.element(screen.getByTestId("next-run")).toHaveTextContent("Runs only when you start it.");
  await screen.getByLabelText("Schedule (cron, UTC)").fill("0 8 * * 1");
  const when = new Date("2026-11-02T08:00:00+00:00").toLocaleString();
  await expect.element(screen.getByTestId("next-run")).toHaveTextContent(`Next run ${when}`);
  await expect.element(screen.getByTestId("schedule")).toMatchTextContent(/0 8 \* \* 1.*Next run/);
  await screen.getByRole("tab", { name: "YAML" }).click();
  const yaml = screen.getByLabelText("Workflow YAML").element() as HTMLTextAreaElement;
  expect(yaml.value).toMatch(/schedule: ["']?0 8 \* \* 1["']?\n/);
});

test("Richard drafts through the planner endpoint and his YAML becomes the editor's text", async () => {
  const sent: unknown[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds": () => Response.json([video]),
    "POST /api/worlds/world_1/workflows/check": check,
    "POST /api/worlds/world_1/planner": async (req) => {
      const body = (await req.json()) as { message: string; session_id: string | null };
      sent.push(body);
      if (body.message.startsWith("Save")) {
        return Response.json({
          reply: "Saved as version 1.",
          yaml: planned, // the API fills it from the saved version
          session_id: "session-1",
          saved: { ...v1, yaml: planned, model: { ...model, name: "Weekly short" } },
        });
      }
      return Response.json({ reply: "Here is a draft.", yaml: planned, session_id: "session-1", saved: null });
    },
    "GET /api/workflows/wf_1/versions": () => Response.json([{ ...v1, yaml: planned }]),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/new");
  await screen.getByRole("tab", { name: "Richard" }).click();
  await screen.getByLabelText("Message to Richard").fill("A weekly short, I pick the idea.");
  await screen.getByRole("button", { name: "Send" }).click();
  await expect.element(screen.getByTestId("chat-log")).toMatchTextContent(/You.*A weekly short.*Richard.*Here is a draft\./);
  await expect.element(screen.getByTestId("check-status")).toMatchTextContent(/Richard updated the draft/);
  expect(sent[0]).toMatchObject({ message: "A weekly short, I pick the idea.", yaml: STARTER, workflow_id: null, session_id: null });

  await screen.getByRole("tab", { name: "YAML" }).click();
  await expect.element(screen.getByLabelText("Workflow YAML")).toHaveValue(planned);

  await screen.getByRole("tab", { name: "Richard" }).click();
  await screen.getByLabelText("Message to Richard").fill("Save it.");
  await screen.getByRole("button", { name: "Send" }).click();
  await expect.element(screen.getByTestId("chat-log")).toMatchTextContent(/Saved as version 1\./);
  expect(sent[1]).toMatchObject({ message: "Save it.", yaml: planned, session_id: "session-1" });
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v1/);
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_1");
  await expect.element(screen.getByTestId("check-status")).toMatchTextContent(/^Saved as version 1;/);
});

test("a planner that does not answer is an alert and the message stays to resend", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds": () => Response.json([video]),
    "POST /api/worlds/world_1/workflows/check": check,
    "POST /api/worlds/world_1/planner": () =>
      Response.json({ detail: "The planner did not answer: the claude process died." }, { status: 502 }),
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new");
  await screen.getByRole("tab", { name: "Richard" }).click();
  await screen.getByLabelText("Message to Richard").fill("Hello");
  await screen.getByRole("button", { name: "Send" }).click();
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/did not answer.*Send it again\./);
  await expect.element(screen.getByLabelText("Message to Richard")).toHaveValue("Hello");
});

test("copying puts the workflow in another world and keeps you here, with a link to the copy", async () => {
  const other = { ...video, id: "world_2", name: "Marketing" };
  let copied: unknown = null;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_2": () => Response.json(other),
    "GET /api/worlds": () => Response.json([video, other]),
    "POST /api/worlds/world_1/workflows/check": check,
    "POST /api/worlds/world_2/workflows/check": check,
    "GET /api/workflows/wf_1/versions": () => Response.json([v1]),
    "GET /api/workflows/wf_9/versions": () => Response.json([{ ...v1, id: "wfv_9", workflow_id: "wf_9" }]),
    "POST /api/workflows/wf_1/copy": async (req) => {
      copied = await req.json();
      return Response.json({ ...v1, id: "wfv_9", workflow_id: "wf_9" }, { status: 201 });
    },
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/wf_1");
  await screen.getByRole("button", { name: "Copy to another world" }).click();
  const item = screen.getByRole("menuitem", { name: /Marketing/ });
  await expect.element(item).toHaveTextContent("MarketingCreates a copy there; you stay here");
  await item.click();
  await expect.element(screen.getByTestId("copied")).toMatchTextContent(/Copied to Marketing as a new workflow at version 1/);
  expect(copied).toEqual({ world_id: "world_2" });
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_1");
  await expect
    .element(screen.getByRole("link", { name: "Open the copy" }))
    .toHaveAttribute("href", "/worlds/world_2/workflows/wf_9");
});

test("a copy the other world refuses names what it lacks", async () => {
  const other = { ...video, id: "world_2", name: "Marketing" };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds": () => Response.json([video, other]),
    "POST /api/worlds/world_1/workflows/check": check,
    "GET /api/workflows/wf_1/versions": () => Response.json([v1]),
    "POST /api/workflows/wf_1/copy": () =>
      Response.json(
        { violations: [{ rule: "unknown_connector", message: "no connector named 'render_video'", stage: "render" }], model: null },
        { status: 422 },
      ),
  });
  const { screen, router } = await mountAt("/worlds/world_1/workflows/wf_1");
  await screen.getByRole("button", { name: "Copy to another world" }).click();
  await screen.getByRole("menuitem", { name: /Marketing/ }).click();
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/cannot run this workflow yet: no connector named 'render_video'/);
  expect(router.state.location.pathname).toBe("/worlds/world_1/workflows/wf_1");
});

test("on a phone the editor shows the stages read-only and says to edit on a larger screen", async () => {
  await page.viewport(390, 844);
  fakeFetch({
    "GET /api/worlds": () => Response.json([video]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/workflows/wf_1/versions": () => Response.json([v1]),
    "POST /api/worlds/world_1/workflows/check": check,
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/wf_1");
  await expect.element(screen.getByRole("heading", { name: "Weekly short v1", level: 1 })).toBeVisible();
  await expect.element(screen.getByText("Edit on a larger screen.")).toBeVisible();
  const rows = screen.getByTestId("stage-rows").getByRole("listitem");
  await expect.element(rows.first()).toMatchTextContent(/swarm.*research.*Find 5 short-form.*makes list\[idea\]/);
  await expect.element(rows.nth(1)).toMatchTextContent(/gate.*pick.*Pick one idea/);
  expect(screen.getByTestId("stage-rows").getByRole("button").elements()).toHaveLength(0);
  expect(screen.getByRole("textbox").elements()).toHaveLength(0);
  expect(screen.getByRole("tab").elements()).toHaveLength(0);
  expect(screen.getByRole("button", { name: /^Save as version/ }).elements()).toHaveLength(0);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});
