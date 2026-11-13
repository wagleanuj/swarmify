import { expect, test } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";
import { CountTick } from "./CountTick";

test("the first value just shows; a change slides the old out and the new in, the way it went", async () => {
  const screen = await render(<CountTick value={3} />);
  const tick = () => screen.container.querySelector(".tick")!;
  expect(tick().textContent).toBe("3");
  expect(tick().querySelector(".tick-in, .tick-out")).toBeNull();

  await screen.rerender(<CountTick value={4} />);
  expect(tick().getAttribute("data-dir")).toBe("up");
  expect(tick().querySelector(".tick-in")?.textContent).toBe("4");
  // The leaving number is hidden from readers and goes when its slide ends.
  expect(tick().querySelector(".tick-out")?.getAttribute("aria-hidden")).toBe("true");
  await expect.poll(() => tick().querySelector(".tick-out")).toBeNull();
  expect(tick().textContent).toBe("4");

  await screen.rerender(<CountTick value={1} />);
  expect(tick().getAttribute("data-dir")).toBe("down");
});

test("a format writes the number out, and the direction still comes from the number", async () => {
  const money = (n: number) => `$${n.toFixed(2)}`;
  const screen = await render(<CountTick value={0.5} format={money} />);
  const tick = () => screen.container.querySelector(".tick")!;
  expect(tick().textContent).toBe("$0.50");
  await screen.rerender(<CountTick value={1.25} format={money} />);
  expect(tick().getAttribute("data-dir")).toBe("up");
  expect(tick().querySelector(".tick-in")?.textContent).toBe("$1.25");
});
