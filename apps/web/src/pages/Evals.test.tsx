import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { components } from "../api/schema";
import { FakeEventSource, fakeFetch } from "../test/fakes";
import { mountAt } from "../test/mount";

type Report = components["schemas"]["EvalReport"];
type Trial = components["schemas"]["Trial"];

const trial = (arm: string, index: number, failed: string[] = []): Trial => ({
  task_id: "home-espresso",
  arm,
  index,
  world_id: `world_${arm}_${index}`,
  run_id: `run_${arm}_${index}`,
  status: "done",
  cost_usd: arm === "swarm" ? 1.2 : 0.4,
  duration_s: arm === "swarm" ? 240 : 600,
  failed,
});
const report = (id: string, created: string, verdict: string): Report => ({
  id,
  created_at: created,
  k: 2,
  tasks: 1,
  swarm_wins: true,
  verdict,
  arms: [
    { arm: "baseline", trials: 2, accepted: 1, acceptance: 0.5, pass_at_k: 1, pass_pow_k: 0, cost_usd: 0.4, duration_s: 600 },
    { arm: "swarm", trials: 2, accepted: 2, acceptance: 1, pass_at_k: 1, pass_pow_k: 1, cost_usd: 1.2, duration_s: 240 },
  ],
  trials: [trial("baseline", 0), trial("swarm", 0), trial("baseline", 1, ["script.md is missing"]), trial("swarm", 1)],
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the newest report says whether the swarm wins and compares the arms; earlier ones list below", async () => {
  fakeFetch({
    "GET /api/evals": () =>
      Response.json([
        report("eval_2", "2026-10-27T10:00:00+00:00", "The swarm wins: it accepted 100% of trials."),
        report("eval_1", "2026-10-20T10:00:00+00:00", "The swarm does not win: it accepted 0% of trials."),
      ]),
  });
  const { screen } = await mountAt("/evals");
  await expect.element(screen.getByTestId("verdict")).toHaveTextContent("The swarm wins: it accepted 100% of trials.");
  const arms = screen.getByTestId("arm");
  await expect.element(arms.first()).toMatchTextContent(/Hooli baseline.*2.*50% \(1\).*100%.*0%.*\$0\.40.*10 min/);
  await expect.element(arms.nth(1)).toMatchTextContent(/Swarm.*2.*100% \(2\).*100%.*100%.*\$1\.20.*4 min/);
  await screen.getByText("Every trial (4)").click();
  const trials = screen.getByTestId("trial");
  expect(trials.elements()).toHaveLength(4);
  await expect.element(trials.nth(2)).toMatchTextContent(/home-espresso.*Hooli baseline.*2.*script\.md is missing/);
  await expect.element(trials.nth(1)).toMatchTextContent(/accepted/);
  await expect
    .element(trials.nth(1).getByRole("link"))
    .toHaveAttribute("href", "/worlds/world_swarm_0/runs/run_swarm_0");
  await expect.element(screen.getByTestId("earlier")).toMatchTextContent(/The swarm does not win/);
});

test("with no eval yet the page says how to run one, and the worlds page links here", async () => {
  fakeFetch({ "GET /api/evals": () => Response.json([]), "GET /api/worlds": () => Response.json([]) });
  const { screen } = await mountAt("/evals");
  await expect.element(screen.getByTestId("no-evals")).toMatchTextContent(/uv run swarmify-eval --k 3/);
  const worlds = await mountAt("/");
  await expect.element(worlds.screen.getByRole("link", { name: "Evals" })).toHaveAttribute("href", "/evals");
});
