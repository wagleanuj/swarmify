import { expect, test } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";

import { Loading, Skeleton } from "./Skeleton";

test("skeletons are hidden blocks in a busy region that says Loading to a screen reader", async () => {
  const screen = await render(
    <Loading>
      <Skeleton w={120} h={22} />
      <Skeleton lines={3} />
    </Loading>,
  );
  const region = screen.container.querySelector("[aria-busy]")!;
  expect(region.getAttribute("aria-busy")).toBe("true");
  await expect.element(screen.getByRole("status")).toHaveTextContent("Loading");
  await expect.element(screen.getByRole("status")).toHaveClass("visually-hidden");
  expect(screen.getByText("Loading...").query()).toBeNull();

  const blocks = region.querySelectorAll<HTMLElement>(".sk");
  expect(blocks).toHaveLength(4);
  for (const b of region.querySelectorAll("[aria-hidden]")) expect(b.getAttribute("aria-hidden")).toBe("true");
  expect(region.querySelectorAll(":scope > [aria-hidden='true']")).toHaveLength(2);
  const first = blocks[0]!.getBoundingClientRect();
  expect([first.width, first.height]).toEqual([120, 22]);
});
