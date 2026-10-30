import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";

import "../index.css";

import type { components } from "../api/schema";
import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";
import { deleteVoiceAsk, plainName, toWav } from "./Assets";

type Voice = components["schemas"]["Voice"];

const world = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const voice = (name: string, over: Partial<Voice> = {}): Voice => ({
  name,
  description: `${name} voice`,
  tags: ["warm"],
  engine: "chatterbox",
  consent: `synthetic: Kokoro preset ${name}, Apache 2.0`,
  preset: name,
  clip: `assets/voices/${name}.wav`,
  seconds: 9.1,
  used_by: [],
  ...over,
});
const heart = voice("af_heart", { used_by: [{ id: "wf_1", name: "Bedtime" }, { id: "wf_2", name: "Weekly short" }] });
const mine = voice("narrator", { consent: "My own recording.", preset: null, seconds: 6.4, tags: ["calm", "bedtime"], description: "Warm and calm, for bedtime facts" });
const fable = voice("bm_fable", { tags: ["british", "narrator"] });
const files = [
  { path: "assets/bed-calm.mp3", name: "bed-calm.mp3", bytes: 2_400_000 },
  { path: "assets/logo.png", name: "logo.png", bytes: 12_000 },
  { path: "assets/fonts/Inter.ttf", name: "Inter.ttf", bytes: 310_000 },
  { path: "assets/voices/af_heart.wav", name: "af_heart.wav", bytes: 400_000 },
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
    "GET /api/worlds/world_1/voices": () => Response.json({ voices: [heart, mine, fable], warnings: [] }),
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

test("voices are playable cards, the operator's own first, before any form; files are rows with previews", async () => {
  routes();
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByRole("heading", { name: "Assets", level: 1 })).toBeVisible();
  await expect.element(screen.getByText("Voices your speech steps can use, and files your builds can read.")).toBeVisible();
  await expect.element(screen.getByRole("listitem", { name: "narrator" })).toBeVisible();
  const cards = screen.getByRole("listitem").elements().filter((li) => li.classList.contains("voice-card"));
  expect(cards.map((c) => c.getAttribute("aria-label"))).toEqual(["narrator", "af_heart", "bm_fable"]);

  const narrator = screen.getByRole("listitem", { name: "narrator" });
  await expect.element(narrator).toMatchTextContent(/6\.4 s clip · chatterbox/);
  await expect.element(narrator).toMatchTextContent(/Not used by a workflow yet/);
  await expect.element(screen.getByRole("listitem", { name: "af_heart" })).toMatchTextContent(/Used by Bedtime and Weekly short/);
  const play = narrator.getByRole("button", { name: "Play narrator" });
  // Play comes before Add a voice in the page
  const add = screen.getByRole("button", { name: "Add a voice" });
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

test("deleting a voice confirms naming the workflows that lose it", async () => {
  const deleted: string[] = [];
  routes({
    "DELETE /api/worlds/world_1/voices/af_heart": (req) => {
      deleted.push(new URL(req.url).pathname);
      return new Response(null, { status: 204 });
    },
  });
  const asked: string[] = [];
  vi.spyOn(window, "confirm").mockImplementation((m) => (asked.push(m ?? ""), asked.length > 1));
  const { screen } = await mountAt("/worlds/world_1/assets");
  const card = screen.getByRole("listitem", { name: "af_heart" });
  for (let i = 0; i < 2; i++) {
    await card.getByRole("button", { name: "af_heart actions" }).click();
    await card.getByRole("menuitem", { name: /^Delete/ }).click();
  }
  expect(asked[0]).toBe(
    "Delete af_heart? Bedtime and Weekly short use it, and their speech step will no longer find this voice. Its clip and catalog entry go.",
  );
  expect(deleted).toEqual(["/api/worlds/world_1/voices/af_heart"]);
  await expect.element(screen.getByRole("status")).toHaveTextContent("Deleted af_heart.");
  expect(deleteVoiceAsk({ name: "x", used_by: [] })).toBe("Delete x? Its clip and catalog entry go. No workflow uses it.");
});

test("a voice's description and tags are edited in its card", async () => {
  let body: unknown;
  routes({
    "PATCH /api/worlds/world_1/voices/narrator": async (req) => {
      body = await req.json();
      return Response.json(mine);
    },
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  const card = screen.getByRole("listitem", { name: "narrator" });
  await card.getByRole("button", { name: "narrator actions" }).click();
  await card.getByRole("menuitem", { name: /^Edit/ }).click();
  await card.getByLabelText("Description").fill("Softer");
  await card.getByLabelText("Tags").fill("soft, bedtime");
  await card.getByRole("button", { name: "Save" }).click();
  await expect.element(screen.getByRole("status")).toHaveTextContent("Saved narrator.");
  expect(body).toEqual({ description: "Softer", tags: ["soft", "bedtime"] });
});

test("Add a voice uploads a clip converted to WAV with its entry and consent", async () => {
  let form: FormData | undefined;
  routes({
    "POST /api/worlds/world_1/voices": async (req) => {
      form = await req.formData();
      return Response.json(mine, { status: 201 });
    },
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await screen.getByRole("button", { name: "Add a voice" }).click();
  const sheet = screen.getByRole("dialog", { name: "Add a voice" });
  const save = sheet.getByRole("button", { name: "Add voice" });
  await expect.element(save).toBeDisabled();

  await sheet.getByLabelText("Clip file").upload(new File([wav(3)], "Short take.wav"));
  await expect.element(sheet.getByRole("alert")).toMatchTextContent(/^This clip is 3\.0 s\. Chatterbox clones from 5 to 10 s of speech/);
  await sheet.getByLabelText("Clip file").upload(new File([wav(6.5)], "My Voice.wav"));
  await expect.element(sheet.getByText("6.5 s")).toBeVisible();
  await expect.element(sheet.getByLabelText("Name")).toHaveValue("Short-take");
  await sheet.getByLabelText("Name").fill("af_heart");
  await expect.element(sheet).toMatchTextContent(/A voice is already named af_heart\./);
  await sheet.getByLabelText("Name").fill("me");
  await sheet.getByLabelText("Description").fill("My own voice");
  await sheet.getByLabelText("Tags").fill("warm, me");
  await expect.element(save).toBeDisabled(); // consent first
  await sheet.getByLabelText("This is my voice, or I have the speaker's permission").click();
  await save.click();
  await expect.element(screen.getByRole("status")).toMatchTextContent(/^Added me\./);
  expect(form?.get("name")).toBe("me");
  expect(form?.get("tags")).toBe("warm, me");
  expect(form?.get("consent")).toMatch(/^This is my voice, or I have the speaker's permission \(uploaded in Swarmify, \d{4}-\d{2}-\d{2}\)$/);
  const clip = form?.get("clip") as File;
  expect(clip.name).toBe("me.wav");
  const head = new Uint8Array(await clip.arrayBuffer()).slice(0, 4);
  expect(String.fromCharCode(...head)).toBe("RIFF");
  expect((await toWav(clip)).seconds).toBeCloseTo(6.5, 1);
});

test("Record uses the microphone through MediaRecorder and keeps the take as the clip", async () => {
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
  await screen.getByRole("button", { name: "Add a voice" }).click();
  const sheet = screen.getByRole("dialog", { name: "Add a voice" });
  await sheet.getByRole("button", { name: "Record" }).click();
  await expect.element(sheet.getByText(/^On a quiet morning/)).toBeVisible();
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

test("empty voices and files name what goes there", async () => {
  routes({
    "GET /api/worlds/world_1/voices": () => Response.json({ voices: [], warnings: ["voice 1 in voices.json is ignored: x has no consent"] }),
    "GET /api/worlds/world_1/assets": () => Response.json([]),
  });
  const { screen } = await mountAt("/worlds/world_1/assets");
  await expect.element(screen.getByText(/No voices yet\. A voice is a 5 to 10 s clip/)).toBeVisible();
  await expect.element(screen.getByText("mise run voices:seed -- world_1")).toBeVisible();
  await expect.element(screen.getByText("voice 1 in voices.json is ignored: x has no consent")).toBeVisible();
  await expect.element(screen.getByText("Put images, music or fonts here for your builds to use.")).toBeVisible();
  await expect.element(screen.getByLabelText("Upload files")).toBeInTheDocument();
});

test("on a phone the sections stack, nothing scrolls sideways, and Add a voice is pinned above the tab bar", async () => {
  await page.viewport(390, 844);
  routes();
  const { screen } = await mountAt("/worlds/world_1/assets");
  const narrator = screen.getByRole("listitem", { name: "narrator" });
  await expect.element(narrator.getByRole("button", { name: "Play narrator" })).toBeVisible();
  await expect.element(screen.getByRole("listitem", { name: "fonts/Inter.ttf" })).toBeInTheDocument();
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
  const cards = [heart, mine, fable].map((v) => screen.getByRole("listitem", { name: v.name }).element().getBoundingClientRect());
  expect(new Set(cards.map((r) => r.left)).size).toBe(1); // one column

  const add = screen.getByRole("button", { name: "Add a voice" });
  const tabbar = screen.getByRole("navigation", { name: "Tabs" }).element().getBoundingClientRect();
  const bar = add.element().parentElement?.getBoundingClientRect();
  expect(Math.round(bar?.bottom ?? 0)).toBe(Math.round(tabbar.top));

  // A card's menu opens inside the screen
  await screen.getByRole("listitem", { name: "af_heart" }).getByRole("button", { name: "af_heart actions" }).click();
  const menu = screen.getByRole("menu", { name: "af_heart actions" }).element().getBoundingClientRect();
  expect(menu.left).toBeGreaterThanOrEqual(0);
  expect(menu.right).toBeLessThanOrEqual(390);

  await add.click();
  const sheet = screen.getByRole("dialog", { name: "Add a voice" });
  await expect.element(sheet.getByRole("button", { name: "Upload clip" })).toBeVisible();
  expect(sheet.element().getBoundingClientRect().bottom).toBeLessThanOrEqual(tabbar.top + 1);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
});

test("an uploaded file gets a name the API takes", () => {
  expect(plainName("Calm Bed (loop).mp3")).toBe("Calm-Bed-loop-.mp3");
  expect(plainName(".hidden.wav")).toBe("hidden.wav");
  expect(plainName("logo.png")).toBe("logo.png");
});
