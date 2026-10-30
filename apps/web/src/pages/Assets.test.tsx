import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
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
  const add = screen.getByRole("button", { name: "Add to Voices" });
  expect(play.element().compareDocumentPosition(add.element()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
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

test("a file in an Owned Folder is renamed in place and deleted after a confirm naming its package", async () => {
  const calls = routes({
    "PATCH /api/worlds/world_1/assets/assets%2Fvoices%2Fnarrator.wav": () => Response.json({ path: "assets/voices/calm.wav", name: "calm.wav", bytes: 300_000 }),
    "DELETE /api/worlds/world_1/assets/assets%2Fvoices%2Faf_heart.wav": () => new Response(null, { status: 204 }),
  });
  const asked: string[] = [];
  vi.spyOn(window, "confirm").mockImplementation((m) => (asked.push(m ?? ""), asked.length > 1));
  const { screen } = await mountAt("/worlds/world_1/assets");
  const narrator = screen.getByRole("listitem", { name: "narrator.wav" });
  await narrator.getByRole("button", { name: "narrator.wav actions" }).click();
  await narrator.getByRole("menuitem", { name: /^Rename/ }).click();
  await narrator.getByLabelText("New name for narrator.wav").fill("calm.wav");
  await narrator.getByRole("button", { name: "Rename" }).click();
  await expect.element(screen.getByRole("status")).toHaveTextContent("Renamed narrator.wav to calm.wav.");

  const card = screen.getByRole("listitem", { name: "af_heart.wav" });
  for (let i = 0; i < 2; i++) {
    await card.getByRole("button", { name: "af_heart.wav actions" }).click();
    await card.getByRole("menuitem", { name: /^Delete/ }).click();
  }
  expect(asked[0]).toBe("Delete af_heart.wav? Speech will no longer find it. This cannot be undone.");
  await expect.element(screen.getByRole("status")).toHaveTextContent("Deleted af_heart.wav.");
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
  await expect.element(screen.getByRole("status")).toHaveTextContent("Added me.wav to Voices.");
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
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen } = await mountAt("/worlds/world_1/assets");
  const logo = screen.getByRole("listitem", { name: "logo.png" });
  await logo.getByRole("button", { name: "logo.png actions" }).click();
  await logo.getByRole("menuitem", { name: /^Rename/ }).click();
  await logo.getByLabelText("New name for logo.png").fill("brand.png");
  await logo.getByRole("button", { name: "Rename" }).click();
  await expect.element(screen.getByRole("status")).toHaveTextContent("Renamed logo.png to brand.png.");
  const bed = screen.getByRole("listitem", { name: "bed-calm.mp3" });
  await bed.getByRole("button", { name: "bed-calm.mp3 actions" }).click();
  await bed.getByRole("menuitem", { name: /^Delete/ }).click();
  expect(window.confirm).toHaveBeenCalledWith("Delete bed-calm.mp3? Builds that read it will not find it. This cannot be undone.");
  await expect.element(screen.getByRole("status")).toHaveTextContent("Deleted bed-calm.mp3.");
  expect(calls).toContain("DELETE /api/worlds/world_1/assets/assets%2Fbed-calm.mp3");
});

test("an empty Owned Folder and empty files say what goes there", async () => {
  routes({ "GET /api/worlds/world_1/assets": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByText(`Nothing in Voices yet. ${voices.about}`)).toBeVisible();
  await expect.element(screen.getByText("Put images, music or fonts here for your builds to use.")).toBeVisible();
  await expect.element(screen.getByLabelText("Upload files")).toBeInTheDocument();
});

test("a world with no Owned Folders shows only its files", async () => {
  routes({ "GET /api/worlds/world_1/owned_folders": () => Response.json([]) });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByRole("listitem", { name: "voices/voices.json" })).toBeVisible();
  expect(screen.getByRole("button", { name: /^Add to/ }).elements()).toHaveLength(0);
});

test("on a phone the sections stack, nothing scrolls sideways, and Add to Voices is pinned above the tab bar", async () => {
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
  const tabbar = screen.getByRole("navigation", { name: "Tabs" }).element().getBoundingClientRect();
  const bar = add.element().parentElement?.getBoundingClientRect();
  expect(Math.round(bar?.bottom ?? 0)).toBe(Math.round(tabbar.top));

  // A card's menu opens inside the screen
  await screen.getByRole("listitem", { name: "af_heart.wav" }).getByRole("button", { name: "af_heart.wav actions" }).click();
  const menu = screen.getByRole("menu", { name: "af_heart.wav actions" }).element().getBoundingClientRect();
  expect(menu.left).toBeGreaterThanOrEqual(0);
  expect(menu.right).toBeLessThanOrEqual(390);

  await add.click();
  const sheet = screen.getByRole("dialog", { name: "Add to Voices" });
  await expect.element(sheet.getByRole("button", { name: "Upload" })).toBeVisible();
  expect(sheet.element().getBoundingClientRect().bottom).toBeLessThanOrEqual(tabbar.top + 1);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("an uploaded file gets a name the API takes", () => {
  expect(plainName("Calm Bed (loop).mp3")).toBe("Calm-Bed-loop-.mp3");
  expect(plainName(".hidden.wav")).toBe("hidden.wav");
  expect(plainName("logo.png")).toBe("logo.png");
});
