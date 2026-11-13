import { afterEach, expect, test, vi } from "vitest";
import { page } from "vitest/browser";
import { render } from "vitest-browser-react";

import "../index.css";

import { fakeFetch } from "../test/fakes";
import { SignIn } from "./SignIn";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("on a phone the password field is 16px, so iOS does not zoom, and Sign in is a 44px tap", async () => {
  await page.viewport(390, 844);
  const screen = await render(<SignIn onSignedIn={() => undefined} />);
  const field = screen.getByLabelText("Password", { exact: true }).element();
  expect(getComputedStyle(field).fontSize).toBe("16px");
  expect(screen.getByRole("button", { name: "Sign in" }).element().getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
  expect(document.documentElement.scrollWidth).toBeLessThanOrEqual(390);
  await page.viewport(1280, 800);
});

test("the password can be shown and hidden, autofills as the current password, and a refusal shakes once and says what to do", async () => {
  fakeFetch({ "POST /api/session": () => Response.json({ detail: "no" }, { status: 401 }) });
  const screen = await render(<SignIn onSignedIn={() => undefined} />);
  const field = screen.getByLabelText("Password", { exact: true });
  await expect.element(field).toHaveAttribute("autocomplete", "current-password");
  await expect.element(field).toHaveAttribute("type", "password");
  await screen.getByRole("button", { name: "Show password" }).click();
  await expect.element(field).toHaveAttribute("type", "text");
  await expect.element(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute("aria-pressed", "true");
  await screen.getByRole("button", { name: "Hide password" }).click();
  await expect.element(field).toHaveAttribute("type", "password");

  expect(screen.container.querySelector(".mark-in svg")).not.toBeNull();
  expect(screen.container.querySelector("form.shake")).toBeNull();
  await field.fill("wrong");
  await screen.getByRole("button", { name: "Sign in" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("That is not the password. Use SWARMIFY_PASSWORD from the install's .env.");
  const form = screen.container.querySelector("form")!;
  expect(form.classList.contains("shake")).toBe(true);
  // Once: the animation does not repeat.
  expect(getComputedStyle(form).animationIterationCount).toBe("1");
});
