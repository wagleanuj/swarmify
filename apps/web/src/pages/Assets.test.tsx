import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { answerConfirm, mountAt } from "../test/mount";
import { plainName, toWav } from "./Assets";

const world = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
/** The speech package's Owned Folder, as its Manifest declares it. */
const voices = {
  path: "assets/voices/",
  folder: "voices",
  label: "Voices",
  about: "Each clip is a voice your speech steps can clone, named by its file.",
  accept: ["audio/wav"],
  confirm: "This is my voice, or I have the speaker's permission",
  package: "Speech",
  templates: false,
  require: [],
};
const files = [
  { path: "assets/bed-calm.mp3", name: "bed-calm.mp3", bytes: 2_400_000 },
  { path: "assets/logo.png", name: "logo.png", bytes: 12_000 },
  { path: "assets/fonts/Inter.ttf", name: "Inter.ttf", bytes: 310_000 },
  { path: "assets/voices/af_heart.wav", name: "af_heart.wav", bytes: 400_000 },
  { path: "assets/voices/narrator.wav", name: "narrator.wav", bytes: 300_000 },
  { path: "assets/voices/voices.json", name: "voices.json", bytes: 900 },
];

/** A 16-bit mono WAV of a tone, `seconds` long. */
function wav(seconds: number, rate = 8000): Blob {
  const n = Math.round(seconds * rate);
  const view = new DataView(new ArrayBuffer(44 + n * 2));
  const text = (at: number, s: string) => [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  text(0, "RIFF");
  view.setUint32(4, 36 + n * 2, true);
  text(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) view.setInt16(44 + i * 2, Math.sin(i / 3) * 9000, true);
  return new Blob([view.buffer], { type: "audio/wav" });
}

function routes(over: Record<string, (req: Request) => Response | Promise<Response>> = {}) {
  return fakeFetch({
    "GET /api/worlds": () => Response.json([world]),
    "GET /api/gates": () => Response.json([]),
    "GET /api/worlds/world_1/owned_folders": () => Response.json([voices]),
    "GET /api/worlds/world_1/assets": () => Response.json(files),
    ...over,
  });
}

beforeEach(async () => {
  FakeEventSource.install();
  await page.viewport(1440, 900);
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("a package's Owned Folder is a group of playable cards before its Add; other files are rows with previews", async () => {
  routes();
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByRole("heading", { name: "Assets", level: 1 })).toBeVisible();
  await expect.element(screen.getByText("Folders your connectors own, and files your builds can read.")).toBeVisible();
  const group = screen.getByRole("region", { name: "Voices" });
  await expect.element(group.getByRole("listitem", { name: "narrator.wav" })).toBeVisible();
  await expect.element(group).toMatchTextContent(/3 files/);
  const cards = group.getByRole("listitem").elements();
  expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["af_heart.wav", "narrator.wav", "voices.json"]);

  const narrator = group.getByRole("listitem", { name: "narrator.wav" });
  await expect.element(narrator).toMatchTextContent(/narrator\s*WAV · 300 KB/);
  // a file the browser cannot play opens instead
  await expect.element(group.getByRole("listitem", { name: "voices.json" }).getByRole("link", { name: "Open" })).toHaveAttribute("href", "/api/worlds/world_1/files/assets/voices/voices.json");
  const play = narrator.getByRole("button", { name: "Play narrator" });
  // Add sits in the section's own header, before its cards, as a text action, not a filled bar
  const add = group.getByRole("button", { name: "Add to Voices" });
  expect(play.element().compareDocumentPosition(add.element()) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  expect(add.element().closest(".panel-head")).not.toBeNull();
  expect(add.element().classList).not.toContain("primary");
  await play.click();
  await expect.element(narrator.getByRole("button", { name: "Pause narrator" })).toHaveAttribute("aria-pressed", "true");
  expect(document.querySelector<HTMLAudioElement>("audio[hidden]")?.src).toMatch(/\/api\/worlds\/world_1\/files\/assets\/voices\/narrator\.wav$/);

  const rows = screen.getByRole("list").elements().find((l) => l.classList.contains("file-rows"));
  expect([...(rows?.children ?? [])].map((li) => li.getAttribute("aria-label"))).toEqual(["bed-calm.mp3", "logo.png", "fonts/Inter.ttf"]);
  const logo = screen.getByRole("listitem", { name: "logo.png" });
  await expect.element(logo).toMatchTextContent(/12 KB/);
  expect(logo.element().querySelector("img")?.getAttribute("src")).toBe("/api/worlds/world_1/files/assets/logo.png");
  await expect.element(screen.getByRole("listitem", { name: "bed-calm.mp3" }).getByLabelText("Play bed-calm.mp3")).toBeVisible();
  await expect.element(screen.getByRole("listitem", { name: "bed-calm.mp3" })).toMatchTextContent(/2\.4 MB/);
  await expect.element(logo.getByRole("link", { name: "Open" })).toHaveAttribute("href", "/api/worlds/world_1/files/assets/logo.png");
});

test("a package's Templates folder is a group of Templates, one row each, not loose files", async () => {
  const remotion = {
    path: "assets/remotion_templates/",
    folder: "remotion_templates",
    label: "Remotion templates",
    about: "Projects a build starts from instead of the starter.",
    accept: [],
    confirm: "",
    package: "Remotion build",
    templates: true,
    require: ["package.json"],
  };
  const at = { folder: "remotion_templates", run_id: "", stage: "" };
  const kids = { ...at, name: "kids", path: "assets/remotion_templates/kids/", bytes: 1_200_000, origin: "folder", added: "2026-10-30T09:00:00+00:00" };
  const office = { ...at, name: "office", path: "assets/remotion_templates/office/", bytes: 85_000_000, origin: "run", run_id: "run_1", stage: "build", added: "2026-11-01T10:00:00+00:00" };
  routes({
    "GET /api/worlds/world_1/owned_folders": () => Response.json([voices, remotion]),
    "GET /api/worlds/world_1/assets": () => Response.json([...files, { path: "assets/remotion_templates/kids/package.json", name: "package.json", bytes: 2 }]),
    "GET /api/worlds/world_1/templates": () => Response.json([kids, office]),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const group = screen.getByRole("region", { name: "Remotion templates" });
  await expect.element(group.getByRole("listitem", { name: "kids" })).toBeVisible();
  expect(group.getByRole("listitem").elements().map((r) => r.getAttribute("aria-label"))).toEqual(["kids", "office"]);
  const added = (iso: string) => new Date(iso).toLocaleDateString();
  await expect.element(group.getByRole("listitem", { name: "kids" })).toMatchTextContent(`kids1.2 MB · Placed by hand · added ${added(kids.added)}`);
  await expect.element(group.getByRole("listitem", { name: "office" })).toMatchTextContent(`office85.0 MB · Saved from run run_1, stage build · added ${added(office.added)}`);
  await expect.element(screen.getByRole("listitem", { name: "bed-calm.mp3" })).toBeVisible();
  expect(screen.getByRole("listitem", { name: /package\.json/ }).elements()).toEqual([]);
});

test("a Template's row shows its prepare status, the error when it failed, and turns ready when its prepare ends", async () => {
  const remotion = { ...voices, path: "assets/remotion_templates/", folder: "remotion_templates", label: "Remotion templates", about: "", accept: [], confirm: "", package: "Remotion build", templates: true, require: [], prepare: ["npm", "ci"], prepare_network: ["registry.npmjs.org"] };
  const at = { folder: "remotion_templates", bytes: 1_000, origin: "upload", run_id: "", stage: "", added: "2026-11-01T10:00:00+00:00", fingerprint: "ab12", prepare_error: "" };
  const row = (name: string, prepare: string, prepare_error = "") => ({ ...at, name, path: `assets/remotion_templates/${name}/`, prepare, prepare_error });
  let asked = 0;
  routes({
    "GET /api/worlds/world_1/owned_folders": () => Response.json([remotion]),
    "GET /api/worlds/world_1/templates": () =>
      Response.json([
        row("cats", ++asked > 1 ? "ready" : "preparing"),
        row("dogs", "failed", "Remotion build's prepare command `npm ci` failed (1): no package-lock.json"),
        row("hand", ""),
      ]),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const group = screen.getByRole("region", { name: "Remotion templates" });
  await expect.element(group.getByRole("listitem", { name: "cats" })).toMatchTextContent(/Preparing/);
  await expect.element(group.getByRole("listitem", { name: "dogs" })).toMatchTextContent(/Prepare failed: Remotion build's prepare command `npm ci` failed \(1\): no package-lock\.json/);
  await expect.element(group.getByRole("listitem", { name: "hand" })).not.toMatchTextContent(/Prepar|Ready/);
  await expect.element(group.getByRole("listitem", { name: "cats" }), { timeout: 5000 }).toMatchTextContent(/Ready/);
});

test("Add a Template uploads a zip under a name from its file, replacing one of that name only when asked", async () => {
  const remotion = { ...voices, path: "assets/remotion_templates/", folder: "remotion_templates", label: "Remotion templates", about: "Projects a build starts from.", accept: [], confirm: "", package: "Remotion build", templates: true, require: ["package.json", "src/index.ts"] };
  const kids = { folder: "remotion_templates", name: "kids", path: "assets/remotion_templates/kids/", bytes: 1_200_000, origin: "upload", run_id: "", stage: "", added: "2026-10-30T09:00:00+00:00", fingerprint: "ab12" };
  const puts: Request[] = [];
  routes({
    "GET /api/worlds/world_1/owned_folders": () => Response.json([remotion]),
    "GET /api/worlds/world_1/templates": () => Response.json([kids]),
    "PUT /api/worlds/world_1/templates/remotion_templates/Cat-Shorts": (req) => {
      puts.push(req);
      return Response.json({ detail: "the Template lacks src/index.ts, which Remotion build requires" }, { status: 422 });
    },
    "PUT /api/worlds/world_1/templates/remotion_templates/kids": (req) => {
      puts.push(req);
      return Response.json(kids, { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await screen.getByRole("region", { name: "Remotion templates" }).getByRole("button", { name: "Add a Template" }).click();
  const sheet = screen.getByRole("dialog", { name: "Add to Remotion templates" });
  await expect.element(sheet).toMatchTextContent(/It must hold package\.json, src\/index\.ts\./);
  const save = sheet.getByRole("button", { name: "Upload", exact: true });
  await expect.element(save).toBeDisabled();
  await sheet.getByLabelText("Zip").upload(new File(["PK"], "Cat Shorts.zip", { type: "application/zip" }));
  await expect.element(sheet.getByLabelText("Name")).toHaveValue("Cat-Shorts");
  await save.click();
  await expect.element(sheet.getByRole("alert")).toHaveTextContent("Not uploaded: the Template lacks src/index.ts, which Remotion build requires.");
  expect(new URL(puts[0]?.url ?? "").search).toBe("");

  await sheet.getByLabelText("Name").fill("kids");
  await expect.element(sheet).toMatchTextContent(/kids is already in Remotion templates\./);
  await expect.element(save).toBeDisabled();
  await sheet.getByLabelText("Replace kids").click();
  await save.click();
  await expect.element(screen.getByText("Replaced kids in Remotion templates.", { exact: true })).toBeVisible(); // a toast
  expect(new URL(puts[1]?.url ?? "").search).toBe("?replace=true");
  expect(await puts[1]!.text()).toBe("PK");
});

test("a world with no Templates says so in its Templates group", async () => {
  const hyper = { ...voices, path: "assets/hyperframes_templates/", folder: "hyperframes_templates", label: "HyperFrames templates", about: "Projects a build starts from.", accept: [], confirm: "", package: "HyperFrames build", templates: true };
  routes({
    "GET /api/worlds/world_1/owned_folders": () => Response.json([hyper]),
    "GET /api/worlds/world_1/templates": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const group = screen.getByRole("region", { name: "HyperFrames templates" });
  await expect.element(group).toMatchTextContent(/No Templates in HyperFrames templates yet\./);
  // what the folder is for waits behind the info glyph
  await expect.element(group.getByText("Projects a build starts from.")).not.toBeVisible();
  await group.getByText("No Templates in HyperFrames templates yet.").click();
  await expect.element(group.getByText("Projects a build starts from.")).toBeVisible();
});

test("a Template downloads as a zip and deletes after a confirm listing the workflows that name it", async () => {
  const remotion = { ...voices, path: "assets/remotion_templates/", folder: "remotion_templates", label: "Remotion templates", about: "", accept: [], confirm: "", package: "Remotion build", templates: true };
  const kids = { name: "kids", folder: "remotion_templates", path: "assets/remotion_templates/kids/", bytes: 1_200_000, origin: "upload", run_id: "", stage: "", added: "2026-10-30T09:00:00+00:00" };
  const url = "DELETE /api/worlds/world_1/templates/remotion_templates/kids";
  const deletes: string[] = [];
  routes({
    "GET /api/worlds/world_1/owned_folders": () => Response.json([remotion]),
    "GET /api/worlds/world_1/templates": () => Response.json([kids]),
    [url]: (req) => {
      const confirm = new URL(req.url).searchParams.get("confirm") === "true";
      deletes.push(confirm ? "confirmed" : "asked");
      return Response.json({ deleted: confirm, workflows: [{ id: "wf_1", name: "Kids short" }, { id: "wf_2", name: "Cat loop" }] });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const row = screen.getByRole("listitem", { name: "kids" });
  await expect.element(row.getByRole("link", { name: "Download kids" })).toHaveAttribute("href", "/api/worlds/world_1/templates/remotion_templates/kids/download");

  await row.getByRole("button", { name: "Delete kids" }).click();
  expect(await answerConfirm(screen, "Cancel")).toContain(
    "Delete the Template kids?2 workflows name it and will fail their check: Kids short, Cat loop.Past runs keep their copies. This cannot be undone.",
  );
  expect(deletes).toEqual(["asked"]);

  await row.getByRole("button", { name: "Delete kids" }).click();
  await answerConfirm(screen, "Delete");
  await expect.element(screen.getByText("Deleted kids.", { exact: true })).toBeVisible(); // a toast
  expect(deletes).toEqual(["asked", "asked", "confirmed"]);
});

test("a file in an Owned Folder is renamed in place and deleted after a confirm naming its package", async () => {
  const calls = routes({
    "PATCH /api/worlds/world_1/assets/assets%2Fvoices%2Fnarrator.wav": () => Response.json({ path: "assets/voices/calm.wav", name: "calm.wav", bytes: 300_000 }),
    "DELETE /api/worlds/world_1/assets/assets%2Fvoices%2Faf_heart.wav": () => new Response(null, { status: 204 }),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const narrator = screen.getByRole("listitem", { name: "narrator.wav" });
  await narrator.getByRole("button", { name: "narrator.wav actions" }).click();
  await narrator.getByRole("menuitem", { name: /^Rename/ }).click();
  await narrator.getByLabelText("New name for narrator.wav").fill("calm.wav");
  await narrator.getByRole("button", { name: "Rename" }).click();
  await expect.element(screen.getByText("Renamed narrator.wav to calm.wav.", { exact: true })).toBeVisible(); // a toast

  const card = screen.getByRole("listitem", { name: "af_heart.wav" });
  const asked: string[] = [];
  for (const answer of ["Cancel", "Delete"]) {
    await card.getByRole("button", { name: "af_heart.wav actions" }).click();
    await card.getByRole("menuitem", { name: /^Delete/ }).click();
    asked.push(await answerConfirm(screen, answer));
  }
  expect(asked[0]).toContain("Delete af_heart.wav?Speech will no longer find it.This cannot be undone.");
  await expect.element(screen.getByText("Deleted af_heart.wav.", { exact: true })).toBeVisible(); // a toast
  expect(calls.filter((c) => c.startsWith("DELETE"))).toEqual(["DELETE /api/worlds/world_1/assets/assets%2Fvoices%2Faf_heart.wav"]);
});

test("Add to Voices uploads a file converted to WAV into the folder, after the package's confirm", async () => {
  let put: Request | undefined;
  routes({
    "PUT /api/worlds/world_1/assets/me.wav": (req) => {
      put = req;
      return Response.json({ path: "assets/voices/me.wav", name: "me.wav", bytes: 1 }, { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await screen.getByRole("button", { name: "Add to Voices" }).click();
  const sheet = screen.getByRole("dialog", { name: "Add to Voices" });
  await expect.element(sheet.getByText(voices.about)).toBeVisible();
  const save = sheet.getByRole("button", { name: "Add", exact: true });
  await expect.element(save).toBeDisabled();

  await sheet.getByLabelText("File").upload(new File([wav(6.5)], "My Voice.wav"));
  await expect.element(sheet.getByText("6.5 s")).toBeVisible();
  await expect.element(sheet.getByLabelText("Name")).toHaveValue("My-Voice");
  await sheet.getByLabelText("Name").fill("narrator");
  await expect.element(sheet).toMatchTextContent(/narrator is already in Voices\./);
  await sheet.getByLabelText("Name").fill("me");
  await expect.element(save).toBeDisabled(); // the confirm first
  await sheet.getByLabelText(voices.confirm).click();
  await save.click();
  await expect.element(screen.getByText("Added me.wav to Voices.", { exact: true })).toBeVisible(); // a toast
  expect(new URL(put?.url ?? "").search).toBe("?folder=voices");
  const body = await put!.blob();
  expect(String.fromCharCode(...new Uint8Array(await body.arrayBuffer()).slice(0, 4))).toBe("RIFF");
  expect((await toWav(body)).seconds).toBeCloseTo(6.5, 1);
});

test("Record uses the microphone through MediaRecorder and keeps the take", async () => {
  const tracks = [{ stop: vi.fn() }];
  vi.stubGlobal("navigator", { ...navigator, mediaDevices: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => tracks }) } });
  class FakeRecorder {
    state = "inactive";
    mimeType = "audio/wav";
    ondataavailable: ((e: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;
    start() {
      this.state = "recording";
    }
    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: wav(7) });
      this.onstop?.();
    }
  }
  vi.stubGlobal("MediaRecorder", FakeRecorder);
  routes();
  const { screen } = await mountAt("/worlds/world_1/assets");
  await screen.getByRole("button", { name: "Add to Voices" }).click();
  const sheet = screen.getByRole("dialog", { name: "Add to Voices" });
  await sheet.getByRole("button", { name: "Record" }).click();
  await sheet.getByRole("button", { name: "Start recording" }).click();
  await sheet.getByRole("button", { name: /^Stop/ }).click();
  await expect.element(sheet.getByText("7.0 s")).toBeVisible();
  await expect.element(sheet.getByRole("button", { name: "Record again" })).toBeVisible();
  expect(tracks[0]?.stop).toHaveBeenCalled();
});

test("files are renamed and deleted with a confirm; an empty folder says what goes there", async () => {
  const calls = routes({
    "PATCH /api/worlds/world_1/assets/assets%2Flogo.png": () => Response.json({ path: "assets/brand.png", name: "brand.png", bytes: 12_000 }),
    "DELETE /api/worlds/world_1/assets/assets%2Fbed-calm.mp3": () => new Response(null, { status: 204 }),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const logo = screen.getByRole("listitem", { name: "logo.png" });
  await logo.getByRole("button", { name: "logo.png actions" }).click();
  await logo.getByRole("menuitem", { name: /^Rename/ }).click();
  await logo.getByLabelText("New name for logo.png").fill("brand.png");
  await logo.getByRole("button", { name: "Rename" }).click();
  await expect.element(screen.getByText("Renamed logo.png to brand.png.", { exact: true })).toBeVisible(); // a toast
  const bed = screen.getByRole("listitem", { name: "bed-calm.mp3" });
  await bed.getByRole("button", { name: "bed-calm.mp3 actions" }).click();
  await bed.getByRole("menuitem", { name: /^Delete/ }).click();
  expect(await answerConfirm(screen, "Delete")).toContain("Delete bed-calm.mp3?Builds that read it will not find it.This cannot be undone.");
  await expect.element(screen.getByText("Deleted bed-calm.mp3.", { exact: true })).toBeVisible(); // a toast
  expect(calls).toContain("DELETE /api/worlds/world_1/assets/assets%2Fbed-calm.mp3");
});

test("an empty Owned Folder and empty files say what goes there", async () => {
  routes({ "GET /api/worlds/world_1/assets": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByText("Nothing in Voices yet.")).toBeVisible();
  await expect.element(screen.getByText(voices.about)).not.toBeVisible();
  await expect.element(screen.getByText("Put images, music or fonts here for your builds to use.")).toBeVisible();
  await expect.element(screen.getByLabelText("Upload files")).toBeInTheDocument();
});

test("a world with no Owned Folders shows only its files", async () => {
  routes({ "GET /api/worlds/world_1/owned_folders": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByRole("listitem", { name: "voices/voices.json" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^Add to/ }).elements()).toHaveLength(0);
});

test("on a phone the sections stack, nothing scrolls sideways, and Add to Voices sits in its section's header, nothing pinned", async () => {
  await page.viewport(390, 844);
  routes();
  const { screen } = await mountAt("/worlds/world_1/assets");
  const narrator = screen.getByRole("listitem", { name: "narrator.wav" });
  await expect.element(narrator.getByRole("button", { name: "Play narrator" })).toBeVisible();
  await expect.element(screen.getByRole("listitem", { name: "fonts/Inter.ttf" })).toBeInTheDocument();
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
  const cards = ["af_heart.wav", "narrator.wav", "voices.json"].map((name) => screen.getByRole("listitem", { name }).element().getBoundingClientRect());
  expect(new Set(cards.map((r) => r.left)).size).toBe(1); // one column

  const add = screen.getByRole("button", { name: "Add to Voices" });
  expect(add.element().closest(".panel-head")).not.toBeNull();
  expect(add.element().getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  expect(document.querySelector("main .action-bar")).toBeNull();
  expect(document.querySelectorAll("main .primary")).toHaveLength(0);

  // A card's menu opens inside the screen
  await screen.getByRole("listitem", { name: "af_heart.wav" }).getByRole("button", { name: "af_heart.wav actions" }).click();
  const menu = screen.getByRole("menu", { name: "af_heart.wav actions" }).element().getBoundingClientRect();
  expect(menu.left).toBeGreaterThanOrEqual(0);
  expect(menu.right).toBeLessThanOrEqual(390);

  await add.click();
  const sheet = screen.getByRole("dialog", { name: "Add to Voices" });
  await expect.element(sheet.getByRole("button", { name: "Upload" })).toBeVisible();
  // A bottom sheet: flush to the screen's foot, over the tab bar.
  expect(Math.round(sheet.element().getBoundingClientRect().bottom)).toBe(innerHeight);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("an uploaded file gets a name the API takes", () => {
  expect(plainName("Calm Bed (loop).mp3")).toBe("Calm-Bed-loop-.mp3");
  expect(plainName(".hidden.wav")).toBe("hidden.wav");
  expect(plainName("logo.png")).toBe("logo.png");
});

test("while the folders load each is a group of skeleton rows, not a Loading line", async () => {
  routes({ "GET /api/worlds/world_1/assets": () => new Promise<Response>(() => undefined) });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByRole("heading", { name: "Files" })).toBeVisible();
  expect(screen.container.querySelectorAll("[aria-busy='true'].sk-rows").length).toBeGreaterThanOrEqual(2);
  expect(screen.container.textContent).not.toMatch(/Loading\.\.\.|Loading \w/);
});

test("at 360 wide a Template row is its whole name, then Download and Delete as equal 44px buttons", async () => {
  await page.viewport(360, 740);
  const remotion = { ...voices, path: "assets/remotion_templates/", folder: "remotion_templates", label: "Remotion templates", package: "Remotion build", templates: true };
  const shorts = { folder: "remotion_templates", name: "blocky-shorts-vertical", path: "assets/remotion_templates/blocky-shorts-vertical/", bytes: 1_200_000, origin: "upload", run_id: "", stage: "", added: "2026-10-30T09:00:00+00:00" };
  routes({
    "GET /api/worlds/world_1/owned_folders": () => Response.json([remotion]),
    "GET /api/worlds/world_1/templates": () => Response.json([shorts]),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const row = screen.getByRole("listitem", { name: "blocky-shorts-vertical" });
  await expect.element(row).toBeVisible();
  const name = row.element().querySelector(".file-name .mono")!.getBoundingClientRect();
  const download = row.getByRole("link", { name: "Download blocky-shorts-vertical" }).element().getBoundingClientRect();
  const del = row.getByRole("button", { name: "Delete blocky-shorts-vertical" }).element().getBoundingClientRect();
  expect(name.height).toBeLessThan(30); // one line
  expect(download.top).toBeGreaterThan(name.bottom - 1);
  expect(download.top).toBe(del.top);
  expect(Math.abs(download.width - del.width)).toBeLessThan(1);
  expect(Math.min(download.height, del.height)).toBeGreaterThanOrEqual(44);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(360);
});
