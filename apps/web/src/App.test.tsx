import { render } from "vitest-browser-react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { App } from "./App";
import { FakeEventSource, fakeFetch } from "./test/fakes";

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("signs in with the password, then goes back to sign-in when the session ends", async () => {
  let session = false;
  const tries: unknown[] = [];
  fakeFetch({
    "GET /api/session": () => Response.json({ signed_in: session, sign_in_required: true }),
    "POST /api/session": async (req) => {
      const { password } = (await req.json()) as { password: string };
      tries.push(password);
      session = password === "aviato";
      return session ? new Response(null, { status: 204 }) : Response.json({ detail: "no" }, { status: 401 });
    },
    "GET /api/worlds": () =>
      session ? Response.json([]) : Response.json({ detail: "Sign in to Swarmify." }, { status: 401 }),
    "GET /api/gates": () => Response.json([]),
  });
  const screen = await render(<App />);
  const password = screen.getByLabelText("Password");
  await expect.element(password).toBeVisible();
  await expect.element(screen.getByRole("navigation", { name: "Main" })).not.toBeInTheDocument();

  await password.fill("hooli");
  await screen.getByRole("button", { name: "Sign in" }).click();
  await expect.element(screen.getByRole("alert")).toMatchTextContent(/That is not the password/);
  await expect.element(password).toHaveValue("");

  await password.fill("aviato");
  await screen.getByRole("button", { name: "Sign in" }).click();
  await expect.element(screen.getByRole("navigation", { name: "Main" })).toBeVisible();
  expect(tries).toEqual(["hooli", "aviato"]);

  // The session expires: the next refused call brings the sign-in page back.
  session = false;
  window.dispatchEvent(new Event("swarmify:signed-out"));
  await expect.element(screen.getByLabelText("Password")).toBeVisible();
});

test("says to wait after too many wrong passwords", async () => {
  fakeFetch({
    "GET /api/session": () => Response.json({ signed_in: false, sign_in_required: true }),
    "POST /api/session": () => Response.json({ detail: "slow down" }, { status: 429 }),
  });
  const screen = await render(<App />);
  await screen.getByLabelText("Password").fill("x");
  await screen.getByRole("button", { name: "Sign in" }).click();
  await expect.element(screen.getByRole("alert")).toHaveTextContent("Too many wrong passwords. Try again in a few minutes.");
});
