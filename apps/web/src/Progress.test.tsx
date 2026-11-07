import { afterEach, expect, test, vi } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";

import { api } from "./api/client";
import { Progress } from "./Progress";

const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));

afterEach(() => vi.unstubAllGlobals());

test("the line shows only after 150 ms of a call in flight and clears when it returns", async () => {
  let answer = () => {};
  vi.stubGlobal("fetch", () => new Promise<Response>((r) => (answer = () => r(Response.json([])))));
  const screen = await render(<Progress />);
  const line = screen.getByTestId("route-progress");

  const call = api.GET("/api/worlds");
  await wait(100);
  expect(line.query()).toBeNull();
  await wait(100);
  await expect.element(line).toBeInTheDocument();

  answer();
  await call;
  await expect.element(line).not.toBeInTheDocument();
});

test("a fast call never shows the line", async () => {
  vi.stubGlobal("fetch", async () => Response.json([]));
  const screen = await render(<Progress />);
  await api.GET("/api/worlds");
  await wait(250);
  expect(screen.getByTestId("route-progress").query()).toBeNull();
});
