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
const report = (id: string, created: string, verdict: string, kind: string | null = null): Report => ({
  id,
  kind,
  p_value: 0.5,
  created_at: created,
  k: 2,
  tasks: 1,
  swarm_wins: verdict.startsWith("The swarm wins"),
  verdict,
  arms: [
    { arm: "baseline", trials: 2, accepted: 1, acceptance: 0.5, pass_at_k: 1, pass_pow_k: 0, cost_usd: 0.4, duration_s: 600, wins: 0, losses: 1, ties: 1, cost_per_win: null },
    { arm: "swarm", trials: 2, accepted: 2, acceptance: 1, pass_at_k: 1, pass_pow_k: 1, cost_usd: 1.2, duration_s: 240, wins: 1, losses: 0, ties: 1, cost_per_win: 2.4 },
  ],
  judgements: [
    { task_id: "home-espresso", index: 0, winner: "swarm", reason: "Fresher ideas, sharper hook." },
    { task_id: "home-espresso", index: 1, winner: null, reason: "Both equally good." },
  ],
  trials: [trial("baseline", 0), trial("swarm", 0), trial("baseline", 1, ["script.md is missing"]), trial("swarm", 1)],
});

beforeEach(() => FakeEventSource.install());
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("the newest verdict is the line under the title; each eval compares the arms in one table", async () => {
  fakeFetch({
    "GET /api/evals": () =>
      Response.json([
        report("eval_2", "2026-10-27T10:00:00+00:00", "The swarm wins: it accepted 100% of trials."),
        report("eval_1", "2026-10-20T10:00:00+00:00", "The swarm does not win: it accepted 0% of trials."),
      ]),
  });
  const { screen } = await mountAt("/evals");
  await expect.element(screen.getByRole("heading", { name: "Evals", level: 1 })).toBeVisible();
  await expect.element(screen.getByTestId("verdict")).toHaveTextContent("The swarm wins: it accepted 100% of trials.");
  const arms = screen.getByTestId("eval").first().getByTestId("arm");
  await expect.element(arms.first()).toMatchTextContent(/Baseline.*2.*50% \(1\).*100%.*0%.*\$0\.40.*10 min011no win/);
  await expect.element(arms.nth(1)).toMatchTextContent(/Swarm.*2.*100% \(2\).*100%.*100%.*\$1\.20.*4 min101\$2\.40/);
  const newest = screen.getByTestId("eval").first();
  await newest.getByText("The judge's picks (2)").click();
  const picks = newest.getByTestId("judgement");
  await expect.element(picks.first()).toMatchTextContent(/home-espresso.*1.*Swarm.*Fresher ideas, sharper hook\./);
  await expect.element(picks.nth(1)).toMatchTextContent(/home-espresso.*2.*tie.*Both equally good\./);
  await newest.getByText("Every trial (4)").click();
  const trials = newest.getByTestId("trial");
  expect(trials.elements()).toHaveLength(4);
  await expect.element(trials.nth(2)).toMatchTextContent(/home-espresso.*Baseline.*2.*script\.md is missing/);
  await expect.element(trials.nth(1)).toMatchTextContent(/accepted/);
  await expect
    .element(trials.nth(1).getByRole("link"))
    .toHaveAttribute("href", "/worlds/world_swarm_0/runs/run_swarm_0");
  const evals = screen.getByTestId("eval");
  expect(evals.elements()).toHaveLength(2);
  await expect.element(evals.first()).toMatchTextContent(/Swarm wins/);
  await expect.element(evals.nth(1)).toMatchTextContent(/Swarm does not win.*The swarm does not win: it accepted 0% of trials\./);
  expect(evals.nth(1).getByTestId("arm").elements()).toHaveLength(2);
});

test("each task kind shows the verdict of its newest report, and a kind never run says so", async () => {
  fakeFetch({
    "GET /api/evals": () =>
      Response.json([
        { ...report("eval_3", "2026-10-30T10:00:00+00:00", "The swarm wins on typed lists: 12 of 16.", "typed lists"), p_value: 0.0384 },
        report("eval_2", "2026-10-29T10:00:00+00:00", "The swarm does not win on research briefs.", "research briefs"),
        report("eval_1", "2026-10-28T10:00:00+00:00", "The swarm wins on research briefs: older.", "research briefs"),
      ]),
  });
  const { screen } = await mountAt("/evals");
  const kinds = screen.getByTestId("kind");
  await expect.element(kinds.first()).toMatchTextContent(/Research briefs.*Swarm does not win.*1 of 1 \(100%\).*0\.50.*no comparison/);
  await expect.element(kinds.nth(1)).toMatchTextContent(/Long-form writing.*Not run yet/);
  await expect.element(kinds.nth(2)).toMatchTextContent(/Typed lists.*Swarm wins.*1 of 1.*0\.04/);
  await expect.element(screen.getByText(/more than 60% of the decided pairs/)).toBeVisible();
  await expect.element(screen.getByTestId("eval").first().getByRole("heading", { level: 2 })).toMatchTextContent(/^Typed lists, /);
});

test("with no eval yet the page says how to run one", async () => {
  fakeFetch({ "GET /api/evals": () => Response.json([]) });
  const { screen } = await mountAt("/evals");
  await expect.element(screen.getByTestId("no-evals")).toMatchTextContent(/uv run swarmify-eval --kind research-briefs/);
});
