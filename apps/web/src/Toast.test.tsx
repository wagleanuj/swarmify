import { afterEach, expect, test, vi } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";
import { dismiss, notify as show, Toasts } from "./Toast";

// The notices live in one module-wide stack; each test clears what it showed.
const shown: number[] = [];
const notify = (...args: Parameters<typeof show>) => {
  const id = show(...args);
  shown.push(id);
  return id;
};
afterEach(() => {
  shown.splice(0).forEach(dismiss);
  vi.useRealTimers();
});

test("a notice shows in one polite region and goes after 4 s", async () => {
  vi.useFakeTimers();
  const screen = await render(<Toasts />);
  notify("Saved as version 2.", { tone: "ok" });
  const region = screen.container.querySelector(".toasts")!;
  await expect.element(screen.getByText("Saved as version 2.")).toBeVisible();
  expect(region.getAttribute("aria-live")).toBe("polite");
  expect(region.querySelector(".i-check.i-pop")).not.toBeNull();
  vi.advanceTimersByTime(4000);
  await expect.element(screen.getByText("Saved as version 2.")).not.toBeInTheDocument();
});

test("at most two stack, the newest last; an error stays until dismissed", async () => {
  vi.useFakeTimers();
  const screen = await render(<Toasts />);
  notify("One");
  notify("Two");
  const id = notify("Could not save.", { tone: "error" });
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Could not save.");
  expect([...screen.container.querySelectorAll(".toast-text")].map((t) => t.textContent)).toEqual(["Two", "Could not save."]);
  vi.advanceTimersByTime(10_000);
  await expect.element(screen.getByText("Two")).not.toBeInTheDocument();
  await expect.element(screen.getByRole("alert")).toBeVisible();
  dismiss(id);
  await expect.element(screen.getByRole("alert")).not.toBeInTheDocument();
});

test("its action runs once and dismisses it; the close button dismisses too", async () => {
  const run = vi.fn();
  const screen = await render(<Toasts />);
  notify("Copied.", { action: { label: "Undo", run } });
  await screen.getByRole("button", { name: "Undo" }).click();
  expect(run).toHaveBeenCalledOnce();
  await expect.element(screen.getByText("Copied.")).not.toBeInTheDocument();
  notify("Again.");
  await screen.getByRole("button", { name: "Dismiss" }).click();
  await expect.element(screen.getByText("Again.")).not.toBeInTheDocument();
});

test("on a phone it sits above the tab bar, its buttons 44px taps", async () => {
  const { page } = await import("vitest/browser");
  await page.viewport(390, 844);
  const screen = await render(<Toasts />);
  notify("Saved.", { action: { label: "Open", run: () => undefined } });
  await expect.element(screen.getByText("Saved.")).toBeVisible();
  const box = () => screen.container.querySelector(".toast")!.getBoundingClientRect();
  // Once it has risen into place: --tabbar is 56px plus the safe area on a phone.
  await expect.poll(() => box().bottom).toBe(844 - 56 - 8);
  expect(box().left).toBe(16);
  expect(box().right).toBe(390 - 16);
  for (const b of screen.getByRole("button").elements()) expect(b.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
});
