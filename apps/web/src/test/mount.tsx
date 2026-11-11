import { expect } from "vitest";
import type { Locator } from "vitest/browser";
import { createMemoryHistory, RouterProvider } from "@tanstack/react-router";
import { render } from "vitest-browser-react";

import { createAppRouter } from "../router";

/** Mount the whole app at `path` with an in-memory history. */
export async function mountAt(path: string) {
  const router = createAppRouter(createMemoryHistory({ initialEntries: [path] }));
  const screen = await render(<RouterProvider router={router} />);
  return { router, screen };
}

/** Answer the open confirm (Dialog.tsx) with `button`, its action or Cancel. Resolves to what it
 * asked, its title and lines, for the test to check. */
export async function answerConfirm(screen: Pick<Locator, "getByRole">, button: string): Promise<string> {
  const dialog = screen.getByRole("dialog");
  await expect.element(dialog).toBeVisible();
  const asked = dialog.element().textContent ?? "";
  await dialog.getByRole("button", { name: button, exact: true }).click();
  await expect.element(dialog).not.toBeInTheDocument();
  return asked;
}
