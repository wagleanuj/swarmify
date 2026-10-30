import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

const video = { id: "world_1", name: "Video", purpose: "", created_at: "2026-10-26T10:00:00+00:00" };
const old = { id: "tok_1", name: "Old script", created_at: "2026-10-20T10:00:00+00:00" };

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("creates a token shown once, revokes one and signs out", async () => {
  const signedOut = vi.fn();
  window.addEventListener("swarmify:signed-out", signedOut);
  const calls = fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/tokens": () => Response.json([old]),
    "GET /api/session": () => Response.json({ signed_in: true, sign_in_required: true }),
    "POST /api/tokens": async (req) => {
      const { name } = (await req.json()) as { name: string };
      return Response.json({ id: "tok_2", name, created_at: "2026-10-30T10:00:00+00:00", token: "swm_secret" }, { status: 201 });
    },
    "DELETE /api/tokens/tok_1": () => new Response(null, { status: 204 }),
    "DELETE /api/session": () => new Response(null, { status: 204 }),
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const { screen } = await mountAt("/worlds/world_1/settings");
  const tokens = screen.getByRole("list", { name: "Tokens" });
  await expect.element(tokens).toMatchTextContent(/Old script/);

  await screen.getByRole("textbox", { name: "New token's name" }).fill("Claude Code");
  await screen.getByRole("button", { name: "Create token" }).click();
  await expect.element(screen.getByTestId("new-token")).toHaveTextContent("swm_secret");
  await expect.element(tokens).toMatchTextContent(/Claude Code/);

  await screen.getByRole("button", { name: "Revoke Old script" }).click();
  await expect.element(screen.getByText("Revoked Old script.")).toBeVisible();
  await expect.element(tokens).not.toMatchTextContent(/Old script/);
  expect(screen.getByTestId("new-token").elements()).toHaveLength(0);
  expect(calls).toContain("DELETE /api/tokens/tok_1");

  await screen.getByRole("button", { name: "Sign out" }).click();
  await vi.waitFor(() => expect(signedOut).toHaveBeenCalled());
  expect(calls).toContain("DELETE /api/session");
  window.removeEventListener("swarmify:signed-out", signedOut);
});

test("without a password there is no sign-out, and an empty list says so", async () => {
  fakeFetch({
    "GET /api/worlds/world_1": () => Response.json(video),
    "GET /api/tokens": () => Response.json([]),
    "GET /api/session": () => Response.json({ signed_in: true, sign_in_required: false }),
  });
  const { screen } = await mountAt("/worlds/world_1/settings");
  await expect.element(screen.getByText("No tokens yet.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Sign out" }).elements()).toHaveLength(0);
});
