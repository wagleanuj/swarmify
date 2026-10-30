import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { browser, parseKeys } from "./Connectors";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const base = {
  world_id: null,
  command: [],
  url: null,
  credentials: [],
  builtin: true,
  created_at: null,
  health: null,
  services: [],
  capabilities: [],
  outputs: [],
  missing: [],
  sign_in: [],
};
const builtins = [
  { ...base, id: "builtin_youtube", name: "youtube", kind: "publish", stage_types: ["connector"], about: "Publishes to YouTube.", label: "YouTube" },
];
const fal = {
  ...base,
  id: "package_fal_video",
  world_id: "world_1",
  name: "fal_video",
  kind: "stage",
  command: ["uvx", "fal-mcp"],
  credentials: ["FAL_KEY"],
  stage_types: ["connector"],
  builtin: false,
  about: "An MCP server added on the Connectors page: uvx fal-mcp",
  label: "fal_video",
  missing: ["fal_key"],
  version: "0.0.1",
};

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("lists connectors by kind with their steps and health, and flags a missing key", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([...builtins, fal]),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await expect.element(screen.getByText(/2 connectors, 1 waiting on a key\./)).toBeVisible();
  await expect.element(screen.getByText(/they are Agent Tools Swarmify gives swarm steps/)).toBeVisible();
  expect(screen.getByRole("list", { name: "Tools agents use inside a swarm step" }).elements()).toHaveLength(0);
  await expect
    .element(screen.getByRole("region", { name: "Publishing" }))
    .toMatchTextContent(/Always after a gate\..*YouTube.*connector steps\s*Not installed/);
  const added = screen.getByRole("list", { name: "Steps of their own" }).getByRole("listitem");
  await expect
    .element(added)
    .toMatchTextContent(/fal_video\s*An MCP server added on the Connectors page: uvx fal-mcp · package fal_video 0\.0\.1\s*connector steps\s*Needs setup\s*Set up/);
  await expect.element(added.getByRole("button", { name: "Set up fal_video" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^Remove/ }).elements()).toHaveLength(0);
});

test("tests a draft, shows its tools, and saves it only after a passing test", async () => {
  const bodies: unknown[] = [];
  let listed: object[] = builtins;
  const falPackage = { name: "fal_video", version: "0.0.1", label: "fal_video", kind: "stage", about: fal.about, state: "installed" };
  let installed: (typeof falPackage)[] = [];
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
      installed = [falPackage];
      return Response.json({ ...fal, missing: [] }, { status: 201 });
    },
    "GET /api/packages": () => Response.json(installed),
    "GET /api/worlds/world_1/packages": () => Response.json(installed.map((p) => ({ name: p.name, version: p.version }))),
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
  // Saving installed it as a local package, enabled here.
  await expect
    .element(screen.getByRole("list", { name: "Installed packages" }))
    .toMatchTextContent(/fal_video 0\.0\.1.*Enabled here/);
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
  let youtube = { ...builtins[0], command: ["python", "-m", "swarmify_connectors.youtube"], credentials: keys, missing: keys };
  let sent: unknown = null;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([youtube]),
    "PUT /api/connectors/builtin_youtube/credentials": async (req) => {
      sent = await req.json();
      youtube = { ...youtube, missing: [] };
      return Response.json({ ...youtube, check: { ok: true, message: "Posts to the YouTube channel Hooks Daily." } });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const row = screen.getByTestId("connector-list").getByRole("listitem").nth(0);
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
  const post = { ...builtins[0], id: "builtin_upload_post", name: "upload_post", command: ["npx"], credentials: keys, missing: keys };
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

test("shows whether each Service a package declares is up, as each speech engine", async () => {
  const speech = { ...base, id: "package_speech", world_id: "world_1", name: "speech", kind: "stage", stage_types: ["connector"], command: [".venv/bin/python", "speech.py"], about: "Voices a script.", services: ["engines"], version: "1.0.0", label: "Speech" };
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([...builtins, speech]),
    "GET /api/worlds/world_1/packages/speech/health": () =>
      Response.json([
        { name: "http://localhost:8881", ok: true, message: "up" },
        { name: "http://localhost:8880", ok: false, message: "connection refused" },
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const engines = screen.getByRole("list", { name: "Speech services" });
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
  // Web search and the graph are Agent Tools now (#210), named in a note rather than listed.
  await expect.element(screen.getByText(/Web search and the world's graph are not connectors/)).toBeVisible();
});

const unchanged = {
  from: "1.0.0",
  to: "1.1.0",
  params: { added: [], removed: [], changed: [] },
  setup: { added: [], removed: [], changed: [] },
  required: [],
  permissions: { added: [], removed: [], changed: [] },
  needs: [],
};

test("a switch shows the Manifest diff and waits for a new required Setup field; an uninstall in use says who uses it", async () => {
  const asked: string[] = [];
  const region = { key: "region", label: "Region", type: "string", secret: false, required: true, scope: "world", env: "REGION", help: null, options: null, default: null, min: null, max: null, pattern: null, shared: null, world: null, shared_set: false, world_set: false };
  const pkg = { name: "echo", label: "Echo", kind: "stage", about: "Says it back.", source: "connectors/echo", installed_at: "2026-10-30T10:00:00+00:00", state: "installed", problem: null };
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json(builtins),
    "GET /api/packages": () => Response.json([{ ...pkg, version: "1.0.0" }, { ...pkg, version: "2.0.0" }]),
    "GET /api/worlds/world_1/packages": () => Response.json([{ name: "echo", version: "1.0.0" }]),
    "GET /api/worlds/world_1/packages/echo/switch": () =>
      Response.json({
        ...unchanged,
        to: "2.0.0",
        params: { added: ["speed"], removed: [], changed: ["loud"] },
        setup: { added: ["region"], removed: [], changed: [] },
        required: ["region"],
        permissions: { added: ["network: eu.example.org"], removed: [], changed: [] },
        needs: ["Region"],
      }),
    "GET /api/worlds/world_1/packages/echo/setup": (req) => {
      asked.push(new URL(req.url).search);
      return Response.json({ package: "echo", version: "2.0.0", help_url: null, test: null, fields: [region], missing: ["region"] });
    },
    "DELETE /api/packages/echo/1.0.0": () =>
      Response.json({ detail: "echo 1.0.0 is in use: world Video pins it; Say it v1 in world Video was saved with it" }, { status: 409 }),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  await screen.getByRole("button", { name: "Enable echo 2.0.0" }).click();
  const shown = screen.getByRole("region", { name: "Switch echo to 2.0.0" });
  await expect.element(shown).toMatchTextContent(/Switch Echo from 1\.0\.0 to 2\.0\.0/);
  await expect.element(shown).toMatchTextContent(/Workflow versions saved on 1\.0\.0 keep running it/);
  await expect.element(shown).toMatchTextContent(/Params\s*Added speed\. Changed loud/);
  await expect.element(shown).toMatchTextContent(/Setup fields\s*Added region/);
  await expect.element(shown).toMatchTextContent(/Permissions\s*Added network: eu\.example\.org/);
  await expect.element(shown.getByRole("status")).toHaveTextContent("Fill in Region in Echo 2.0.0's Setup first; the switch waits for it.");
  await expect.element(shown.getByRole("button", { name: "Switch to 2.0.0" })).toBeDisabled();
  await shown.getByRole("button", { name: "Set up echo" }).click();
  await expect.element(shown.getByRole("form", { name: "Setup of Echo 2.0.0" })).toMatchTextContent(/Region · this world/);
  expect(asked).toEqual(["?version=2.0.0"]);
  await shown.getByRole("button", { name: "Keep 1.0.0" }).click();
  expect(screen.getByRole("region", { name: "Switch echo to 2.0.0" }).elements()).toHaveLength(0);
  expect(calls).not.toContain("PUT /api/worlds/world_1/packages/echo");

  await screen.getByRole("button", { name: "Uninstall echo 1.0.0" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent(
    "echo 1.0.0 is in use: world Video pins it; Say it v1 in world Video was saved with it",
  );
});

test("shows which packages this world enables at which version, and enables, switches and disables them", async () => {
  const pkg = { name: "echo", label: "Echo", kind: "stage", about: "Says it back.", source: "connectors/echo", installed_at: "2026-10-30T10:00:00+00:00", state: "installed", problem: null };
  let enabled = [{ name: "echo", version: "1.0.0" }];
  const echo = { ...base, id: "package_echo", world_id: "world_1", name: "echo", kind: "stage", stage_types: ["connector"], builtin: false, about: "Says it back.", command: ["uv", "run", "server.py"], version: "1.0.0", label: "Echo" };
  const bodies: unknown[] = [];
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json(enabled.length ? [...builtins, echo] : builtins),
    "GET /api/packages": () => Response.json([{ ...pkg, version: "1.0.0" }, { ...pkg, version: "1.1.0" }]),
    "GET /api/worlds/world_1/packages": () => Response.json(enabled),
    "PUT /api/worlds/world_1/packages/echo": async (req) => {
      const body = (await req.json()) as { version: string };
      bodies.push(body);
      enabled = [{ name: "echo", ...body }];
      return Response.json(enabled[0]);
    },
    "DELETE /api/worlds/world_1/packages/echo": () => {
      enabled = [];
      return new Response(null, { status: 204 });
    },
    "GET /api/worlds/world_1/packages/echo/switch": () => Response.json(unchanged),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const rows = screen.getByRole("list", { name: "Installed packages" }).getByRole("listitem");
  await expect.element(rows.first()).toMatchTextContent(/echo 1\.0\.0.*Installed\s*Enabled here\s*Disable/);
  await expect.element(rows.last()).toMatchTextContent(/echo 1\.1\.0.*Installed\s*Switch from 1\.0\.0/);
  const steps = screen.getByRole("list", { name: "Steps of their own" });
  await expect.element(steps).toMatchTextContent(/Echo\s*Says it back\. · package echo 1\.0\.0/);
  expect(screen.getByRole("button", { name: "Remove echo" }).elements()).toHaveLength(0);

  await screen.getByRole("button", { name: "Enable echo 1.1.0" }).click();
  const shown = screen.getByRole("region", { name: "Switch echo to 1.1.0" });
  await expect.element(shown).toMatchTextContent(/params, Setup fields and permissions are unchanged/);
  expect(bodies).toEqual([]);
  await shown.getByRole("button", { name: "Switch to 1.1.0" }).click();
  await expect.element(rows.last()).toMatchTextContent(/Enabled here/);
  await expect.element(rows.first()).toMatchTextContent(/Switch from 1\.1\.0/);
  expect(bodies).toEqual([{ version: "1.1.0" }]);

  await screen.getByRole("button", { name: "Disable echo 1.1.0" }).click();
  await expect.element(rows.first()).toMatchTextContent(/Installed\s*Enable\s*Uninstall$/);
  await expect.element(rows.last()).toMatchTextContent(/Installed\s*Enable\s*Uninstall$/);
  expect(calls).toContain("DELETE /api/worlds/world_1/packages/echo");
  expect(screen.getByRole("list", { name: "Steps of their own" }).elements()).toHaveLength(0);
});

test("draws a package's Setup form from its fields, tests before saving and shows what the test said", async () => {
  const field = { secret: false, required: false, scope: "shared", help: null, options: null, default: null, min: null, max: null, pattern: null, shared: null, world: null, shared_set: false, world_set: false };
  const form = {
    package: "probe",
    version: "1.0.0",
    help_url: "https://example.org/probe",
    test: "check_setup",
    missing: ["api_key"],
    fields: [
      { ...field, key: "api_key", label: "API key", type: "string", env: "PROBE_KEY", secret: true, required: true, help: "The check passes for good." },
      { ...field, key: "notes", label: "Notes", type: "text", env: "NOTES" },
      { ...field, key: "ratio", label: "Ratio", type: "number", env: "RATIO", min: 0, max: 1, shared: 0.5, shared_set: true },
      { ...field, key: "retries", label: "Retries", type: "integer", env: "RETRIES", default: 3 },
      { ...field, key: "loud", label: "Loud", type: "boolean", env: "LOUD", default: false },
      { ...field, key: "voice", label: "Voice", type: "choice", env: "VOICE", scope: "world", options: ["low", "high"], default: "low" },
      { ...field, key: "endpoint", label: "Service URL", type: "url", env: "ENDPOINT" },
      { ...field, key: "cache", label: "Cache folder", type: "path", env: "CACHE" },
    ],
  };
  const probe = { ...base, id: "package_probe", world_id: "world_1", name: "probe", kind: "stage", stage_types: ["connector"], builtin: false, about: "Answers with its environment.", command: ["python3", "server.py"], credentials: ["PROBE_KEY"], version: "1.0.0", label: "Probe", missing: ["api_key"] };
  let listed = probe;
  const bodies: unknown[] = [];
  const answers = [
    Response.json({ detail: "Not saved. The test said: That key was refused." }, { status: 422 }),
    Response.json({ ...form, missing: [], check: { ok: true, message: "Signed in as probe." } }),
  ];
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([...builtins, listed]),
    "GET /api/worlds/world_1/packages/probe/setup": () => Response.json(form),
    "PUT /api/worlds/world_1/packages/probe/setup": async (req) => {
      bodies.push(await req.json());
      const answer = answers.shift() as Response;
      if (answer.ok) listed = { ...probe, missing: [] };
      return answer;
    },
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const row = screen.getByRole("list", { name: "Steps of their own" }).getByRole("listitem");
  await expect.element(row).toMatchTextContent(/Probe.*Needs setup\s*Set up/);
  expect(row.getByRole("button", { name: "Set keys for probe" }).elements()).toHaveLength(0);
  await row.getByRole("button", { name: "Set up probe" }).click();

  const setup = screen.getByRole("form", { name: "Setup of Probe" });
  const key = setup.getByLabelText(/^API key · every world/);
  await expect.element(key).toHaveAttribute("type", "password");
  await expect.element(key).toBeRequired();
  await expect.element(setup.getByText("The check passes for good.")).toBeVisible();
  await expect.element(setup.getByRole("textbox", { name: /^Notes.*every world/ })).toBeVisible();
  await expect.element(setup.getByRole("spinbutton", { name: /^Ratio.*every world/ })).toHaveValue(0.5);
  await expect.element(setup.getByRole("spinbutton", { name: /^Retries.*every world/ })).toHaveAttribute("step", "1");
  await expect.element(setup.getByRole("combobox", { name: /^Loud.*every world/ })).toHaveTextContent("Default: NoYesNo");
  await expect.element(setup.getByRole("combobox", { name: /^Voice.*this world/ })).toHaveTextContent("Default: lowlowhigh");
  await expect.element(setup.getByLabelText(/^Service URL.*every world/)).toHaveAttribute("type", "url");
  await expect.element(setup.getByRole("textbox", { name: /^Cache folder.*every world/ })).toBeVisible();
  expect(setup.getByRole("combobox", { name: /^Voice here/ }).elements()).toHaveLength(0); // world-only: no override

  await key.fill("bad");
  await setup.getByRole("combobox", { name: /^Voice.*this world/ }).selectOptions("high");
  await setup.getByRole("button", { name: "Test and save" }).click();
  await expect.element(setup.getByRole("alert")).toHaveTextContent("Not saved. The test said: That key was refused.");
  expect(bodies[0]).toEqual({
    shared: { api_key: "bad", notes: "", ratio: "0.5", retries: "", loud: "", endpoint: "", cache: "" },
    world: { api_key: "", notes: "", ratio: "", retries: "", loud: "", voice: "high", endpoint: "", cache: "" },
  });

  await key.fill("good");
  await setup.getByRole("button", { name: "Test and save" }).click();
  await expect.element(screen.getByRole("status")).toHaveTextContent("Setup saved. Signed in as probe.");
  await expect.element(row).toMatchTextContent(/Probe.*Ready\s*Change setup/);
});

test("an oauth field signs in through the provider and shows when the provider refused it", async () => {
  const field = { required: false, scope: "shared", help: null, options: null, default: null, min: null, max: null, pattern: null, shared: null, world: null, shared_set: false, world_set: false, needs_sign_in: false };
  const form = {
    package: "probe",
    version: "1.0.0",
    help_url: null,
    test: null,
    missing: [],
    fields: [
      { ...field, key: "account_client_id", label: "Account client ID", type: "string", env: null, secret: true, shared_set: true },
      { ...field, key: "account_client_secret", label: "Account client secret", type: "string", env: null, secret: true },
      { ...field, key: "account", label: "Account", type: "oauth", env: "PROBE_TOKEN", secret: true, scope: "world", world_set: true, needs_sign_in: true },
    ],
  };
  const probe = { ...base, id: "package_probe", world_id: "world_1", name: "probe", kind: "stage", stage_types: ["connector"], builtin: false, about: "", command: ["python3", "server.py"], credentials: ["PROBE_TOKEN"], version: "1.0.0", label: "Probe", sign_in: ["account"] };
  const go = vi.spyOn(browser, "go").mockImplementation(() => undefined);
  let started = 0;
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/worlds/world_1/connectors": () => Response.json([...builtins, probe]),
    "GET /api/worlds/world_1/packages/probe/setup": () => Response.json(form),
    "POST /api/worlds/world_1/packages/probe/setup/oauth/account": () =>
      ++started === 1
        ? Response.json({ detail: "Account: save its client ID first, then sign in" }, { status: 422 })
        : Response.json({ url: "https://auth.example.org/authorize?state=s", redirect_uri: "x" }),
  });
  const { screen } = await mountAt("/worlds/world_1/connectors");
  const row = screen.getByRole("list", { name: "Steps of their own" }).getByRole("listitem");
  await expect.element(row).toMatchTextContent(/Probe.*Needs sign-in/);
  await row.getByRole("button", { name: "Set up probe" }).click();
  const setup = screen.getByRole("form", { name: "Setup of Probe" });
  await expect.element(setup.getByLabelText(/^Account client ID.*every world/)).toHaveAttribute("type", "password");
  await expect.element(setup.getByText(/The provider refused the sign-in\. Probe steps fail until you sign in again\./)).toBeVisible();
  await expect.element(setup.getByText(`${window.location.origin}/api/setup/oauth/callback`)).toBeVisible();
  expect(setup.getByRole("textbox", { name: /^Account ·/ }).elements()).toHaveLength(0);

  const button = setup.getByRole("button", { name: "Sign in to Account" });
  await expect.element(button).toHaveTextContent("Sign in again");
  await button.click();
  await expect.element(setup.getByRole("alert")).toHaveTextContent("Account: save its client ID first, then sign in");
  await button.click();
  await vi.waitFor(() => expect(go).toHaveBeenCalledWith("https://auth.example.org/authorize?state=s"));
});

test("the page says how a sign-in went when the provider sends the browser back", async () => {
  fakeFetch({ "GET /api/worlds/world_1": () => Response.json(video) });
  const { screen, router } = await mountAt("/worlds/world_1/connectors?sign_in=probe");
  await expect.element(screen.getByRole("status")).toHaveTextContent("Signed in. probe gets a fresh access token each time it starts.");
  await vi.waitFor(() => expect(router.state.location.search).toEqual({}));
});
