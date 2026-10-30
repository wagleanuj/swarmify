import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { parseKeys } from "./Connectors";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const base = {
  world_id: null,
  command: [],
  url: null,
  credentials: [],
  builtin: true,
  created_at: null,
  health: null,
  missing: [],
};
const builtins = [
  { ...base, id: "builtin_web_search", name: "web_search", kind: "tool", stage_types: ["swarm"], about: "Built into Claude. No key." },
  { ...base, id: "builtin_graph", name: "graph", kind: "tool", stage_types: ["swarm"], about: "This world's graph, read only." },
  { ...base, id: "builtin_youtube", name: "youtube", kind: "publish", stage_types: ["connector"], about: "Publishes to YouTube." },
];
const fal = {
  ...base,
  id: "conn_1",
  world_id: "world_1",
  name: "fal_video",
  kind: "stage",
  command: ["uvx", "fal-mcp"],
  credentials: ["FAL_KEY"],
  stage_types: ["connector"],
  builtin: false,
  about: "",
  missing: ["FAL_KEY"],
};

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists connectors by kind with their steps and health, and flags a missing key with Set keys", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([...builtins, fal]),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await expect.element(screen.getByText(/4 connectors, 1 waiting on a key\./)).toBeVisible();
  const tools = screen.getByRole("list", { name: "Tools agents use inside a swarm step" });
  await expect.element(tools).toMatchTextContent(/Web search\s*Built into Claude\. No key\.\s*swarm steps\s*Ready/);
  await expect.element(tools).toMatchTextContent(/Middle-out/);
  await expect
    .element(screen.getByRole("region", { name: "Publishing" }))
    .toMatchTextContent(/Always after a gate\..*YouTube.*connector steps\s*Not installed/);
  const added = screen.getByRole("list", { name: "Steps of their own" }).getByRole("listitem");
  await expect.element(added).toMatchTextContent(/fal_video\s*uvx fal-mcp · FAL_KEY not set\s*connector steps\s*Needs a key\s*Set keys/);
  await expect.element(added.getByRole("button", { name: "Set keys for fal_video" })).toHaveTextContent("Set keys");
  await expect.element(screen.getByRole("button", { name: "Remove fal_video" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Remove web_search" }).elements()).toHaveLength(0);
});

test("tests a draft, shows its tools, and saves it only after a passing test", async () => {
  const bodies: unknown[] = [];
  let listed: object[] = builtins;
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json(listed),
    "POST /api/worlds/world_1/connectors/test": async (req) => {
      bodies.push(await req.json());
      return Response.json({ tools: [{ name: "make_clip", description: "Make a clip.", input_schema: {} }] });
    },
    "POST /api/worlds/world_1/connectors": async (req) => {
      bodies.push(await req.json());
      listed = [...builtins, { ...fal, missing: [] }];
      return Response.json({ ...fal, missing: [] }, { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await screen.getByRole("button", { name: "Add a connector" }).click();
  await screen.getByRole("textbox", { name: "Name" }).fill("fal_video");
  await screen.getByRole("textbox", { name: "MCP server" }).fill("uvx fal-mcp");
  await screen.getByRole("textbox", { name: /Keys/ }).fill("FAL_KEY=s3cret\nFAL_ORG");
  expect(screen.getByRole("button", { name: "Save connector" }).elements()).toHaveLength(0);
  await screen.getByRole("button", { name: "Test and list tools" }).click();

  await expect.element(screen.getByTestId("tools")).toMatchTextContent(
    /offers 1 tool\. Once saved, connector stages may call it\.\s*make_clip Make a clip\./,
  );
  expect(bodies[0]).toEqual({
    name: "fal_video",
    kind: "stage",
    server: "uvx fal-mcp",
    credentials: { FAL_KEY: "s3cret", FAL_ORG: "" },
    stage_types: ["connector"],
  });

  // Any edit invalidates the test: Save goes away until it passes again.
  await screen.getByRole("checkbox", { name: "swarm stages" }).click();
  expect(screen.getByRole("button", { name: "Save connector" }).elements()).toHaveLength(0);
  await screen.getByRole("checkbox", { name: "swarm stages" }).click();
  await screen.getByRole("button", { name: "Test and list tools" }).click();
  await screen.getByRole("button", { name: "Save connector" }).click();

  await expect.element(screen.getByTestId("connector-list")).toMatchTextContent(/fal_video.*Ready/);
  expect(bodies[2]).toEqual(bodies[0]);
  expect(calls.filter((c) => c === "GET /api/worlds/world_1/connectors")).toHaveLength(2);
  await expect.element(screen.getByRole("textbox", { name: "Name" })).toHaveValue("");
});

test("a failed test says why and offers no save", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json(builtins),
    "POST /api/worlds/world_1/connectors/test": () =>
      Response.json({ detail: "fal_video: FileNotFoundError: uvx" }, { status: 422 }),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await screen.getByRole("button", { name: "Add a connector" }).click();
  await screen.getByRole("textbox", { name: "Name" }).fill("fal_video");
  await screen.getByRole("textbox", { name: "MCP server" }).fill("uvx fal-mcp");
  await screen.getByRole("button", { name: "Test and list tools" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("fal_video: FileNotFoundError: uvx");
  expect(screen.getByRole("button", { name: "Save connector" }).elements()).toHaveLength(0);
});

test("a publish connector is only ever allowed in connector stages", async () => {
  const bodies: unknown[] = [];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json(builtins),
    "POST /api/worlds/world_1/connectors/test": async (req) => {
      bodies.push(await req.json());
      return Response.json({ tools: [] });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await screen.getByRole("button", { name: "Add a connector" }).click();
  await screen.getByRole("checkbox", { name: "swarm stages" }).click();
  await screen.getByRole("radio", { name: /Publish/ }).click();
  await expect.element(screen.getByRole("checkbox", { name: "swarm stages" })).toBeDisabled();
  await expect.element(screen.getByRole("checkbox", { name: "swarm stages" })).not.toBeChecked();
  await screen.getByRole("textbox", { name: "Name" }).fill("tiktok");
  await screen.getByRole("textbox", { name: "MCP server" }).fill("https://mcp.example/mcp");
  await screen.getByRole("button", { name: "Test and list tools" }).click();
  await expect.element(screen.getByTestId("tools")).toMatchTextContent(/offers 0 tools/);
  expect(bodies[0]).toMatchObject({ kind: "publish", stage_types: ["connector"] });
});

test("parseKeys reads NAME=value lines and bare names", () => {
  expect(parseKeys(" A=1 \n\nB\nC = x=y ")).toEqual({ A: "1", B: "", C: "x=y" });
});

test("a built-in publish connector takes its keys in place, and the list shows them saved", async () => {
  const keys = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN"];
  let youtube = { ...builtins[2], command: ["python", "-m", "swarmify_connectors.youtube"], credentials: keys, missing: keys };
  let sent: unknown = null;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([builtins[0], youtube]),
    "PUT /api/connectors/builtin_youtube/credentials": async (req) => {
      sent = await req.json();
      youtube = { ...youtube, missing: [] };
      return Response.json({ ...youtube, check: { ok: true, message: "Posts to the YouTube channel Hooks Daily." } });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const row = screen.getByTestId("connector-list").getByRole("listitem").nth(1);
  await expect.element(row).toMatchTextContent(/YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REFRESH_TOKEN not set.*Needs a key/);
  await row.getByRole("button", { name: "Set keys for youtube" }).click();
  const form = row.getByRole("form", { name: "Keys for youtube" });
  await form.getByLabelText("YOUTUBE_CLIENT_ID").fill("id");
  await form.getByLabelText("YOUTUBE_REFRESH_TOKEN").fill(" r ");
  await form.getByRole("button", { name: "Save keys" }).click();
  await vi.waitFor(() =>
    expect(sent).toEqual({ credentials: { YOUTUBE_CLIENT_ID: "id", YOUTUBE_CLIENT_SECRET: "", YOUTUBE_REFRESH_TOKEN: "r" } }),
  );
  await expect.element(row).toMatchTextContent(/Ready/);
  await expect.element(row.getByRole("status")).toHaveTextContent("Keys saved. Posts to the YouTube channel Hooks Daily.");
  await expect.element(row.getByRole("button", { name: "Set keys for youtube" })).toHaveTextContent("Change keys");
});

test("the page says keys are shared by every world, and a failing key check warns but saves", async () => {
  const keys = ["UPLOAD_POST_API_KEY"];
  const post = { ...builtins[2], id: "builtin_upload_post", name: "upload_post", command: ["npx"], credentials: keys, missing: keys };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([post]),
    "PUT /api/connectors/builtin_upload_post/credentials": () =>
      Response.json({ ...post, missing: [], check: { ok: false, message: "Invalid API key" } }),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await expect.element(screen.getByText(/shared by every world/)).toBeVisible();
  const row = screen.getByTestId("connector-list").getByRole("listitem").first();
  await row.getByRole("button", { name: "Set keys for upload_post" }).click();
  await expect.element(row.getByText(/shared by every world/)).toBeVisible();
  await row.getByLabelText("UPLOAD_POST_API_KEY").fill("k");
  await row.getByRole("button", { name: "Save keys" }).click();
  await expect.element(row.getByRole("alert")).toHaveTextContent(
    "Keys saved, but the check failed: Invalid API key. Publishing fails until they work.",
  );
});

test("shows whether each speech engine is up", async () => {
  const speech = { ...base, id: "builtin_speech", name: "speech", kind: "stage", stage_types: ["connector"], command: ["python"], about: "Voices a script.", health: "speech_health" };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([...builtins, speech]),
    "GET /api/connectors/builtin_speech/health": () =>
      Response.json([
        { name: "http://localhost:8881", ok: true, message: "up" },
        { name: "http://localhost:8880", ok: false, message: "connection refused" },
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const engines = screen.getByRole("list", { name: "Speech engines" });
  await expect.element(engines).toMatchTextContent(/localhost:8881\s*up/);
  await expect.element(engines).toMatchTextContent(/localhost:8880\s*down\s*connection refused/);
});

test("lists installed packages beside the built-ins with version, kind, about and install state", async () => {
  const pkg = { name: "echo", version: "1.0.0", label: "Echo", kind: "stage", source: "connectors/echo", installed_at: "2026-10-30T10:00:00+00:00" };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json(builtins),
    "GET /api/packages": () =>
      Response.json([
        { ...pkg, about: "Says it back.", state: "installed", problem: null },
        { ...pkg, version: "0.9.0", label: "echo", kind: "", about: "", state: "invalid", problem: "kind: 'robot' is not one of [...]" },
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const list = screen.getByRole("list", { name: "Installed packages" });
  await expect.element(list.getByRole("listitem").first()).toMatchTextContent(/Echo\s*Says it back\.\s*echo 1\.0\.0\s*stage\s*Installed/);
  await expect.element(list.getByRole("listitem").last()).toMatchTextContent(/kind: 'robot'.*echo 0\.9\.0\s*Invalid Manifest/);
  await expect.element(screen.getByRole("list", { name: "Tools agents use inside a swarm step" })).toMatchTextContent(/Web search/);
});
