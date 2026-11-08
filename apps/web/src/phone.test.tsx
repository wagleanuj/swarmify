import { afterEach, expect, test } from "vitest";
import { page } from "vitest/browser";
import { render } from "vitest-browser-react";

import "./index.css";

afterEach(() => page.viewport(1280, 800));

const Form = () => (
  <main>
    <input aria-label="Name" />
    <select aria-label="Kind">
      <option>pick</option>
    </select>
    <textarea aria-label="Goal" id="yaml" />
    <button type="button">Save</button>
    <button type="button" className="icon" aria-label="Close">
      x
    </button>
    <button type="button" className="link">
      details
    </button>
  </main>
);

test("on a desktop with a mouse the controls keep their compact size", async () => {
  await page.viewport(1280, 800);
  const screen = await render(<Form />);
  // the stylesheet's media queries catch up with the resize, sometimes after more than a frame
  await expect.poll(() => getComputedStyle(screen.getByLabelText("Name").element()).fontSize).toBe("13px");
  expect(screen.getByRole("button", { name: "Save" }).element().getBoundingClientRect().height).toBeLessThan(44);
});

test("on a phone inputs are 16px, so iOS does not zoom, and controls are a 44px tap", async () => {
  await page.viewport(390, 844);
  const screen = await render(<Form />);
  for (const name of ["Name", "Kind", "Goal"]) expect(getComputedStyle(screen.getByLabelText(name).element()).fontSize).toBe("16px");
  for (const name of ["Save", "Kind"]) expect(screen.getByRole(name === "Kind" ? "combobox" : "button", { name }).element().getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  const close = screen.getByRole("button", { name: "Close" }).element().getBoundingClientRect();
  expect(Math.min(close.width, close.height)).toBeGreaterThanOrEqual(44);
  // An inline text button stays a line of text.
  expect(screen.getByRole("button", { name: "details" }).element().getBoundingClientRect().height).toBeLessThan(30);
});
