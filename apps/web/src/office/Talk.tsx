import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { type CSSProperties, useEffect, useId, useRef, useState } from "react";

import {
  blocked,
  choosable,
  type Choice,
  decision,
  editable,
  gateArtifacts,
  gateProposal,
  type Proposal,
  sendBackTargets,
  staysOpen,
  topUpSuggestion,
  underReview,
} from "../api/answer";
import { api } from "../api/client";
import { labelOf, useConnectors } from "../api/connectors";
import type { Agent, ArtifactFile } from "../api/events";
import { dropGate, reloadGates, useOpenGates } from "../api/gates";
import { type Gate, money, type Run, useRunUpdates } from "../api/runs";
import type { components } from "../api/schema";
import type { Skin } from "../skin";
import { type Look, stillFrame, useCast } from "./cast";
import "./office.css";
import { asker, line } from "./scene";

type PublishPreview = components["schemas"]["PublishPreview"];

/** One answer at a gate: its button, whether the asker cheers (yes) or slumps (stop, send back),
 * and the choice it makes from the form, or null when it should not go (a pick left empty). */
type Option = { label: string; cheer: boolean; disabled?: boolean; choose: (form: FormData) => Choice | null };
type Reaction = { said: string; name: string; look: Look | null; cheer: boolean; n: number };

/** Talk: the askers at Your office, oldest first, one at a time. The asker stands large and says
 * the gate's ask; the choices are the gate's real ones, posted by the shared answering code, so
 * answering here is the same as on the plain Gates page. Number keys answer. */
export function Talk() {
  const { worldId } = useParams({ from: "/worlds/$worldId/office/talk" });
  const search = useSearch({ from: "/worlds/$worldId/office/talk" });
  const navigate = useNavigate();
  const open = useOpenGates();
  const [runs, setRuns] = useState<Run[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [refused, setRefused] = useState<string | null>(null);
  const [last, setLast] = useState<Reaction | null>(null);
  // Answered this visit: a gate opened again after a send back is a new decision, so match the opening too.
  const [answered, setAnswered] = useState<Gate[]>([]);
  const [skin, setSkin] = useState<Skin | undefined>(undefined);
  const { cast, look, nameOf } = useCast(worldId, undefined, skin);
  const asked = useRef(new Set<string>());

  useEffect(() => {
    // The world's skin says which pack and casting dress the askers.
    api
      .GET("/api/worlds/{world_id}", { params: { path: { world_id: worldId } } })
      .then(({ data }) => setSkin(data?.skin ?? undefined))
      .catch(() => undefined);
    api
      .GET("/api/worlds/{world_id}/runs", { params: { path: { world_id: worldId } } })
      .then(({ data }) => data && setRuns(data))
      .catch(() => undefined);
  }, [worldId]);
  useRunUpdates(worldId, (run) => setRuns((prev) => [run, ...prev.filter((r) => r.id !== run.id)]));

  const waiting = line(open.gates, worldId).filter(
    (g) => !answered.some((a) => a.stage_id === g.stage_id && a.opened_at === g.opened_at),
  );
  // Who asks needs the run's agents; read each waiting run's once.
  useEffect(() => {
    for (const runId of new Set(waiting.map((g) => g.run_id))) {
      if (asked.current.has(runId)) continue;
      asked.current.add(runId);
      api
        .GET("/api/runs/{run_id}/agents", { params: { path: { run_id: runId } } })
        .then(({ data }) => data && setAgents((prev) => [...prev, ...data]))
        .catch(() => undefined);
    }
  });

  const planner = cast.find((m) => m.role === "planner")?.name;
  const who = (gate: Gate) => {
    const name = asker(gate, runs, agents, planner);
    return { name: name ? nameOf(name) : "Someone", look: name ? look(name) : null };
  };
  const gate = waiting.find((g) => g.stage_id === search.gate) ?? waiting[0];

  async function answer(g: Gate, send: () => Promise<string | null>, said: string, cheer: boolean) {
    setRefused(null);
    const why = await send();
    if (why) {
      setRefused(why);
      reloadGates();
      return;
    }
    setLast((prev) => ({ said, cheer, ...who(g), n: (prev?.n ?? 0) + 1 }));
    // A budget gate stays until the run resumes, so its asker stays up.
    if (staysOpen(g)) return reloadGates();
    dropGate(g.stage_id);
    setAnswered((a) => [...a, g]);
    const next = waiting.find((w) => w.stage_id !== g.stage_id);
    void navigate({ to: "/worlds/$worldId/office/talk", params: { worldId }, search: next ? { gate: next.stage_id } : {} });
  }

  const moving = runs.filter((r) => !r.parent_run_id && r.status === "running").length;
  return (
    <div className="office-app talk-app">
      <header className="office-bar" aria-label="Talk">
        <Link className="office-btn office-back" to="/worlds/$worldId/office" params={{ worldId }} activeOptions={{ exact: true }}>
          Back to the office
        </Link>
        {gate && (
          <div className="office-chip" data-testid="talk-place">
            {waiting.indexOf(gate) + 1} <span>of {waiting.length} waiting at Your office</span>
          </div>
        )}
      </header>
      {(refused ?? (open.failed && "Could not load the gates. Is the API running?")) && (
        <p className="office-note" role="alert">
          {refused ?? "Could not load the gates. Is the API running?"}
        </p>
      )}
      {last && (
        <p key={last.n} className="office-note talk-said" role="status">
          {last.look && <img src={stillFrame(last.look, last.cheer ? "cheer" : "stuck")} alt={`${last.name} ${last.cheer ? "cheers" : "slumps"}`} />}
          {last.said}.
        </p>
      )}
      <main className="talk-room">
        {gate ? (
          <Ask
            key={`${gate.stage_id}/${gate.opened_at}`}
            gate={gate}
            run={runs.find((r) => r.id === gate.run_id)}
            asker={who(gate)}
            onAnswer={answer}
          />
        ) : open.gates === null ? (
          !open.failed && <p role="status">Loading...</p>
        ) : (
          <section className="talk-clear">
            <h2>All clear</h2>
            <p>
              Nobody needs you. {moving === 1 ? "One run is" : `${moving} runs are`} moving on their own; the next question
              lands here.
            </p>
            <Link className="office-btn" to="/worlds/$worldId/office" params={{ worldId }}>
              Back to the office
            </Link>
          </section>
        )}
      </main>
    </div>
  );
}

/** One asker and their gate: the ask, the run beside it, the publish preview before any choice,
 * and the numbered choices. */
function Ask({
  gate,
  run,
  asker,
  onAnswer,
}: {
  gate: Gate;
  run?: Run;
  asker: { name: string; look: Look | null };
  onAnswer: (gate: Gate, send: () => Promise<string | null>, said: string, cheer: boolean) => Promise<void>;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [busy, setBusy] = useState(false);
  // What the earlier stages made (a pick's cards, what an approve reviews), and a skill gate's proposal.
  const [artifacts, setArtifacts] = useState<ArtifactFile[] | null>(null);
  const [proposal, setProposal] = useState<Proposal | null | undefined>(undefined);
  useEffect(() => {
    if (gate.kind === "pick" || gate.kind === "approve") {
      gateArtifacts(gate)
        .then(setArtifacts)
        .catch(() => setArtifacts([]));
    }
    if (gate.kind === "skill") {
      gateProposal(gate)
        .then(setProposal)
        .catch(() => setProposal(null));
    }
  }, [gate.run_id, gate.stage_index, gate.kind, gate.send_backs_left]);

  const cards = gate.kind === "pick" && artifacts ? choosable(artifacts) : [];
  const reviewed = gate.kind === "approve" && artifacts ? underReview(artifacts) : [];
  const options = choices(gate, artifacts, cards, proposal);

  async function choose(option: Option) {
    if (busy || option.disabled || !form.current) return;
    const fields = new FormData(form.current);
    const choice = option.choose(fields);
    if (!choice) return;
    const edits = Object.fromEntries([...fields].flatMap(([k, v]) => (k.startsWith("edit:") ? [[k.slice(5), String(v)]] : [])));
    const { send, said } = decision(gate, choice, { note: String(fields.get("note") ?? "").trim(), edits, reviewed });
    setBusy(true);
    await onAnswer(gate, send, said, option.cheer);
    setBusy(false);
  }

  // Number keys answer, unless the operator is typing.
  const latest = useRef(choose);
  latest.current = choose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.metaKey || e.ctrlKey || e.altKey) return;
      const option = options[Number(e.key) - 1];
      if (/^[1-9]$/.test(e.key) && option) void latest.current(option);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const spent = gate.spent_usd ?? run?.spent_usd ?? 0;
  const budget = gate.budget_usd ?? run?.budget_usd;
  const label = asker.look?.label;
  return (
    <div className="talk-scene" style={{ "--accent": asker.look?.colour ?? undefined } as CSSProperties}>
      {asker.look && (
        <div className="talk-hero">
          <img src={stillFrame(asker.look, "talk")} alt={[asker.name, label === asker.name ? "" : label, "asking"].filter(Boolean).join(", ")} />
        </div>
      )}
      <section className="talk-box" aria-label={`${asker.name} asks`}>
        <h1 className="talk-name">
          {asker.name}
          {label && label !== asker.name && <> <small>{label}</small></>}
        </h1>
        <p className="talk-line">{gate.ask}</p>
        <ul className="talk-ctx" aria-label="The run">
          <li>
            <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: gate.world_id, runId: gate.run_id }} search={{ stage: gate.stage_id }}>
              {gate.run_name}
            </Link>
          </li>
          <li>
            stage {gate.stage_index + 1}
            {run ? ` of ${run.stages.length}` : ""}, {gate.stage_name}
          </li>
          <li>Spent {money(spent)}{budget == null ? ", no budget" : ` of ${money(budget)}`}</li>
        </ul>
        <form ref={form} className="talk-form" aria-label={`Answer ${gate.stage_name}`} onSubmit={(e) => e.preventDefault()}>
          {gate.publish && <WhatGoesOut preview={gate.publish} worldId={gate.world_id} />}
          {reviewed.length > 0 && <p className="talk-review">Under review: {reviewed.map((a) => a.name).join(", ")}</p>}
          {gate.kind === "skill" && proposal?.skill && (
            <section className="talk-review" aria-label="Skill change">
              <p>
                {proposal.skill.action} skill {proposal.skill.name}: {proposal.skill.summary}
              </p>
              {(proposal.skill.evidence ?? []).length > 0 && <p>Evidence: {(proposal.skill.evidence ?? []).join("; ")}</p>}
              {proposal.skill.action !== "retired" && (
                <label>
                  SKILL.md (edit before approving)
                  <textarea name="skill" rows={4} defaultValue={proposal.skill.content ?? ""} />
                </label>
              )}
            </section>
          )}
          {gate.kind === "skill" && proposal?.workflow && (
            <section className="talk-review" aria-label="Workflow change">
              <p>Workflow change: {proposal.workflow.summary}</p>
              {(proposal.workflow.evidence ?? []).length > 0 && <p>Evidence: {(proposal.workflow.evidence ?? []).join("; ")}</p>}
              <pre>{proposal.workflow.yaml}</pre>
              <label>
                <input type="checkbox" name="apply" defaultChecked /> Save it as the workflow's next version when approving
              </label>
            </section>
          )}
          {gate.kind === "skill" && proposal === null && <p className="talk-review">The proposal could not be loaded; the run keeps waiting.</p>}
          {gate.kind === "pick" && artifacts && cards.length === 0 && (
            <label>
              Your pick
              <input name="choice" autoComplete="off" placeholder="idea 1" />
            </label>
          )}
          {gate.kind === "budget" ? (
            <label>
              Add (US dollars)
              <input name="add" type="number" inputMode="decimal" min="0.01" step="0.01" defaultValue={topUpSuggestion(gate).toFixed(2)} />
            </label>
          ) : (
            <label>
              Note (optional)
              <textarea name="note" rows={1} />
            </label>
          )}
          <ol className="talk-choices" aria-label="Choices">
            {options.map((o, i) => (
              <li key={o.label}>
                <button type="button" className={`office-btn${o.cheer ? " talk-yes" : ""}`} disabled={busy || o.disabled} onClick={() => void choose(o)}>
                  <kbd>{i + 1}</kbd> {o.label}
                </button>
              </li>
            ))}
          </ol>
        </form>
      </section>
    </div>
  );
}

/** The gate's real choices, as the plain Gates page offers them. */
function choices(gate: Gate, artifacts: ArtifactFile[] | null, cards: ArtifactFile[], proposal: Proposal | null | undefined): Option[] {
  const stop = blocked(gate);
  switch (gate.kind) {
    case "pick":
      if (!artifacts) return [];
      if (cards.length === 0) {
        return [
          {
            label: "Go with my pick",
            cheer: true,
            choose: (f) => {
              const choice = String(f.get("choice") ?? "").trim();
              return choice ? { kind: "pick", choice } : null;
            },
          },
        ];
      }
      return cards.map((c) => ({ label: `Pick ${c.title}`, cheer: true, choose: () => ({ kind: "pick", choice: c.title, artifact: c.path }) }));
    case "approve":
      return [
        { label: "Approve", cheer: true, disabled: stop, choose: () => ({ kind: "approve" }) },
        ...sendBackTargets(gate).map((to) => ({ label: `Send back to ${to}`, cheer: false, choose: (): Choice => ({ kind: "send_back", to }) })),
      ];
    case "confirm":
      return [{ label: gate.publish ? "Confirm and post" : "Confirm", cheer: true, disabled: stop, choose: () => ({ kind: "confirm" }) }];
    case "budget":
      return [
        {
          label: "Add money and go on",
          cheer: true,
          choose: (f) => {
            const add = Number(f.get("add"));
            return add > 0 ? { kind: "budget", add } : null;
          },
        },
        {
          label: "Abort the run",
          cheer: false,
          choose: () => (window.confirm(`Abort ${gate.run_name}? Finished stages keep their artifacts.`) ? { kind: "abort" } : null),
        },
      ];
    case "skill": {
      if (!proposal) return [];
      const content = (f: FormData) => String(f.get("skill") ?? proposal.skill?.content ?? "");
      return [
        {
          label: "Approve",
          cheer: true,
          choose: (f) => ({ kind: "skill", proposal, approve: true, content: content(f), applyWorkflow: f.get("apply") !== null }),
        },
        {
          label: "Reject",
          cheer: false,
          choose: (f) =>
            String(f.get("note") ?? "").trim() || window.confirm("Reject without saying why? The retro learns from the reason.")
              ? { kind: "skill", proposal, approve: false, content: content(f), applyWorkflow: false }
              : null,
        },
      ];
    }
    default:
      return [];
  }
}

/** What the publish stage after this gate sends out, as its connector describes it (ADR 0007):
 * shown before any choice. Fields it marks editable are fields of the answer. */
function WhatGoesOut({ preview, worldId }: { preview: PublishPreview; worldId: string }) {
  const id = useId();
  const connectors = useConnectors(worldId);
  const problems = preview.problems ?? [];
  return (
    <section className="talk-publish" aria-label="What goes out">
      <dl>
        <dt>Where</dt>
        <dd>{labelOf(connectors, preview.connector)}</dd>
        {(preview.fields ?? []).map((f) => (
          <div key={f.key}>
            <dt>{editable(f) ? <label htmlFor={`${id}-${f.key}`}>{f.label}</label> : f.label}</dt>
            <dd>
              {editable(f) ? (
                <textarea id={`${id}-${f.key}`} name={`edit:${f.key}`} rows={1} defaultValue={String(f.value ?? "")} />
              ) : (
                shown(f.value)
              )}
            </dd>
          </div>
        ))}
      </dl>
      {problems.length > 0 && (
        <div role="alert">
          <p>This will not post:</p>
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

const shown = (value: unknown) =>
  value === null || value === undefined || value === ""
    ? "None"
    : typeof value === "boolean"
      ? value
        ? "Yes"
        : "No"
      : Array.isArray(value)
        ? value.join(", ")
        : typeof value === "object"
          ? JSON.stringify(value)
          : String(value);
