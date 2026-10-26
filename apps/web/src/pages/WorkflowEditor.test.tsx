import { afterEach, beforeEach, expect, test, vi } from "vitest";

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

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a new workflow opens with a starter, checks it inline and shows the stage rail", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": check,
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new");
  await expect.element(screen.getByRole("heading", { name: "Weekly short", level: 1 })).toBeVisible();
  await expect.element(screen.getByLabelText("Workflow YAML")).toHaveValue(STARTER);
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/No problems/);
  await expect.element(screen.getByTestId("stage-rail")).toMatchTextContent(/research.*swarm.*pick.*gate/);
  await expect.element(screen.getByText("Version 1 is created when you save.")).toBeVisible();
  await expect.element(screen.getByRole("button", { name: "Save as version 1" })).toBeEnabled();
});

test("violations appear inline as you type and block saving until fixed", async () => {
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "POST /api/worlds/world_1/workflows/check": check,
  });
  const { screen } = await mountAt("/worlds/world_1/workflows/new");
  const editor = screen.getByLabelText("Workflow YAML");
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/No problems/);

  await editor.fill(`${STARTER}  - connector: youtube\n`);
  await expect.element(screen.getByRole("heading", { name: "1 problem to fix before saving" })).toBeVisible();
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(
    /youtube: publish connector 'youtube' must come right after a gate/,
  );
  await expect.element(screen.getByRole("button", { name: "Save as version 1" })).toBeDisabled();

  await editor.fill("name: [[\n");
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/not valid YAML at line 2/);
  await expect.element(screen.getByText("The stage list appears as soon as the YAML parses.")).toBeVisible();

  await editor.fill(STARTER);
  await expect.element(screen.getByTestId("problems")).toMatchTextContent(/No problems/);
  await expect.element(screen.getByRole("button", { name: "Save as version 1" })).toBeEnabled();
  expect(calls.filter((c) => c.startsWith("POST /api/worlds/world_1/workflows/check")).length).toBeGreaterThan(2);
});

test("saving a new workflow posts the YAML and opens the saved workflow's editor", async () => {
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
  await expect.element(screen.getByTestId("check-status")).toHaveTextContent("Saved as version 1.");
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
  const { screen } = await mountAt("/worlds/world_1/workflows/wf_1");
  const editor = screen.getByLabelText("Workflow YAML");
  await expect.element(editor).toHaveValue(v2.yaml);
  await expect.element(screen.getByRole("heading", { name: "Daily short", level: 1 })).toBeVisible();
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v2.*open.*v1.*Open v1/);
  await expect.element(screen.getByRole("button", { name: "Save as version 3" })).toBeDisabled();

  await screen.getByRole("button", { name: "Open v1" }).click();
  await expect.element(editor).toHaveValue(STARTER);
  await expect.element(screen.getByTestId("check-status")).toHaveTextContent("Unsaved changes.");
  await expect.element(screen.getByTestId("versions")).toMatchTextContent(/v2.*Open v2.*v1.*open/);
  await editor.fill(`${STARTER}# v3\n`);
  await screen.getByRole("button", { name: "Save as version 3" }).click();
  await expect.element(screen.getByTestId("check-status")).toHaveTextContent("Saved as version 3.");
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
