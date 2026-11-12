import { expect, test } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";
import { Icon, ICONS } from "./Icon";

test("every glyph is an outline on the 24 grid in the text colour, hidden from readers unless labelled", async () => {
  expect(ICONS.length).toBeGreaterThanOrEqual(25);
  const screen = await render(
    <div style={{ color: "rgb(1, 2, 3)" }}>
      {ICONS.map((n) => (
        <Icon key={n} name={n} />
      ))}
      <Icon name="check" size={20} label="Done" />
    </div>,
  );
  const svgs = [...screen.container.querySelectorAll("svg")];
  for (const svg of svgs.slice(0, -1)) {
    expect(svg.getAttribute("viewBox")).toBe("0 0 24 24");
    expect(svg.getAttribute("aria-hidden")).toBe("true");
    expect(getComputedStyle(svg).stroke).toBe("rgb(1, 2, 3)");
    expect(svg.getBoundingClientRect().width).toBe(16);
    expect(svg.querySelector("path")?.getAttribute("d")).toBeTruthy();
  }
  await expect.element(screen.getByRole("img", { name: "Done" })).toBeVisible();
  expect(svgs.at(-1)!.getBoundingClientRect().width).toBe(20);
});

test("a running play glyph carries three live dots; a plain one does not", async () => {
  const screen = await render(
    <>
      <Icon name="play" live />
      <Icon name="play" />
    </>,
  );
  const [live, still] = screen.container.querySelectorAll("svg");
  expect(live!.classList.contains("i-live")).toBe(true);
  expect(live!.querySelectorAll(".i-dots circle")).toHaveLength(3);
  expect(still!.querySelector(".i-dots")).toBeNull();
});
