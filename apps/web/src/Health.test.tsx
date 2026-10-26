import { render } from "vitest-browser-react";
import { afterEach, expect, test, vi } from "vitest";

import { Health } from "./Health";

afterEach(() => vi.restoreAllMocks());

test("renders the version from GET /api/health", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(Response.json({ version: "9.9.9" }));
  const screen = await render(<Health />);
  await expect.element(screen.getByTestId("version")).toHaveTextContent("9.9.9");
  await expect.element(screen.getByRole("status")).toHaveTextContent("API is up, version 9.9.9");
});

test("shows an alert when the API is unreachable", async () => {
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
  const screen = await render(<Health />);
  await expect.element(screen.getByRole("alert")).toHaveTextContent("API is not reachable.");
});
