import { afterEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";
import { render } from "vitest-browser-react";

import "../index.css";

import { SignIn } from "./SignIn";

afterEach(() => vi.restoreAllMocks());

test("on a phone the password field is 16px, so iOS does not zoom, and Sign in is a 44px tap", async () => {
  await page.viewport(390, 844);
  const screen = await render(<SignIn onSignedIn={() => undefined} />);
  const field = screen.getByLabelText("Password").element();
  expect(getComputedStyle(field).fontSize).toBe("16px");
  expect(screen.getByRole("button", { name: "Sign in" }).element().getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
  await page.viewport(1280, 800);
});
