import { afterEach, expect, test, vi } from "vitest";
import { render } from "vitest-browser-react";

import { run, stage } from "./test/fakes";
import { Elapsed } from "./Elapsed";

afterEach(() => vi.useRealTimers());

const started = (status: Parameters<typeof run>[0], ended: string | null = null) =>
  run(status, [stage(0, "research", "swarm", status === "done" ? "done" : "running", { started_at: "2026-10-26T10:00:00Z", ended_at: ended })]);

test("ticks each second while the run is live, and holds once it ends", async () => {
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(new Date("2026-10-26T10:01:05Z"));
  const screen = await render(<Elapsed run={started("running")} />);
  const time = () => screen.container.querySelector("time")!;
  expect(time().textContent).toBe("1m 5s");
  expect(time().dataset.live).toBe("true");
  vi.advanceTimersByTime(1000);
  await vi.waitFor(() => expect(time().textContent).toBe("1m 6s"));

  await screen.rerender(<Elapsed run={started("done", "2026-10-26T10:01:10Z")} />);
  expect(time().textContent).toBe("1m 10s");
  expect(time().dataset.live).toBeUndefined();
  vi.advanceTimersByTime(5000);
  expect(time().textContent).toBe("1m 10s");
});

test("a run that has not started shows no clock", async () => {
  const screen = await render(<Elapsed run={run("queued", [stage(0, "research", "swarm", "pending")])} />);
  expect(screen.container.querySelector("time")).toBeNull();
});
