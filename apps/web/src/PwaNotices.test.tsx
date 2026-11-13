import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";
import { render, renderHook } from "vitest-browser-react";

import "./index.css";

import { earnedInstallHint, markAnswered, setPwa, THEME_COLOUR, useThemeColour } from "./pwa";
import { InstallRow, PwaNotices } from "./PwaNotices";

const Row = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <section aria-label={title}>{children}</section>
);
const offer = () => Object.assign(new Event("beforeinstallprompt"), { prompt: vi.fn(async () => undefined) });

beforeEach(() => {
  localStorage.clear();
  setPwa({ waiting: null, offer: null, online: true, office: false });
});
afterEach(() => page.viewport(1280, 800));

test("offline says so, and a waiting version asks before it reloads", async () => {
  const worker = { postMessage: vi.fn() } as unknown as ServiceWorker;
  setPwa({ online: false, waiting: worker });
  const screen = await render(<PwaNotices />);
  await expect.element(screen.getByText("Offline. Live runs and gates come back when you reconnect.")).toBeVisible();
  await expect.element(screen.getByText("A new version of Swarmify is ready.")).toBeVisible();
  expect(worker.postMessage).not.toHaveBeenCalled();
  await screen.getByRole("button", { name: "Reload" }).click();
  expect(worker.postMessage).toHaveBeenCalledWith("SKIP_WAITING");
});

test("the install hint waits until the app has given something, then goes for good", async () => {
  await page.viewport(390, 844);
  setPwa({ offer: offer() as never });
  expect(earnedInstallHint()).toBe(false);
  markAnswered();
  expect(earnedInstallHint()).toBe(true);
  const screen = await render(<PwaNotices />);
  const hint = screen.getByRole("complementary", { name: "Install Swarmify" });
  await expect.element(hint).toBeVisible();
  await screen.getByRole("button", { name: "No thanks" }).click();
  await expect.element(hint).not.toBeInTheDocument();
  expect(earnedInstallHint()).toBe(false);
});

test("three days of use earn the hint too, never on the Office", async () => {
  await page.viewport(390, 844);
  localStorage.setItem("swarmify.days", JSON.stringify(["2026-01-01", "2026-01-02", "2026-01-03"]));
  setPwa({ offer: offer() as never, office: true });
  const screen = await render(<PwaNotices />);
  expect(screen.getByRole("complementary", { name: "Install Swarmify" }).query()).toBeNull();
});

test("Settings installs from the browser's offer", async () => {
  const held = offer();
  setPwa({ offer: held as never });
  const screen = await render(<InstallRow Row={Row} />);
  await screen.getByRole("button", { name: "Install Swarmify" }).click();
  expect(held.prompt).toHaveBeenCalled();
  await expect.element(screen.getByText(/has not offered an install/)).toBeVisible();
});

test("the theme colour follows the view", async () => {
  const meta = document.head.appendChild(Object.assign(document.createElement("meta"), { name: "theme-color" }));
  const { rerender } = await renderHook((office: boolean | undefined) => useThemeColour(!!office), { initialProps: true });
  expect(meta.content).toBe(THEME_COLOUR.office);
  await rerender(false);
  expect(meta.content).toBe(THEME_COLOUR.plain);
  meta.remove();
});
