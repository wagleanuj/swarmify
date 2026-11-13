import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";

import { stopPinch } from "./pinch";
import { applyTextSize, setTextSize, TextSizePicker, textSize } from "./textSize";

const root = () => parseFloat(getComputedStyle(document.documentElement).fontSize);

beforeEach(() => localStorage.clear());
afterEach(() => {
  vi.restoreAllMocks();
  setTextSize("default");
});

test("Text size scales the root and is kept for this device", async () => {
  applyTextSize();
  expect(textSize()).toBe("default");
  const base = root();
  const screen = await render(<TextSizePicker />);
  await expect.element(screen.getByRole("radio", { name: "Default" })).toBeChecked();

  await screen.getByRole("radio", { name: "Larger" }).click();
  expect(localStorage.getItem("swarmify.textSize")).toBe("larger");
  expect(document.documentElement.dataset.textSize).toBe("larger");
  expect(root()).toBeCloseTo(base * 1.25);

  await screen.getByRole("radio", { name: "Large", exact: true }).click();
  expect(root()).toBeCloseTo(base * 1.125);

  await screen.getByRole("radio", { name: "Default" }).click();
  expect(localStorage.getItem("swarmify.textSize")).toBeNull();
  expect(document.documentElement.dataset.textSize).toBeUndefined();
  expect(root()).toBeCloseTo(base);
});

test("a saved size comes back on load, and blocked storage falls back to Default", () => {
  localStorage.setItem("swarmify.textSize", "large");
  applyTextSize();
  expect(document.documentElement.dataset.textSize).toBe("large");

  localStorage.setItem("swarmify.textSize", "huge");
  expect(textSize()).toBe("default");

  vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(textSize()).toBe("default");
  setTextSize("larger");
  expect(document.documentElement.dataset.textSize).toBe("larger");
});

test("a pinch is stopped, except on an element that opts in", () => {
  const doc = document.implementation.createHTMLDocument();
  stopPinch(doc);
  doc.body.innerHTML = `<p>text</p><img data-pinch alt="">`;
  const pinch = (target: Element, fingers: number) => {
    const e = new Event("touchmove", { bubbles: true, cancelable: true });
    Object.defineProperty(e, "touches", { value: { length: fingers } });
    target.dispatchEvent(e);
    return e.defaultPrevented;
  };
  const p = doc.querySelector("p")!;
  expect(pinch(p, 2)).toBe(true);
  expect(pinch(p, 1)).toBe(false);
  expect(pinch(doc.querySelector("img")!, 2)).toBe(false);
  const gesture = new Event("gesturestart", { bubbles: true, cancelable: true });
  p.dispatchEvent(gesture);
  expect(gesture.defaultPrevented).toBe(true);
});
