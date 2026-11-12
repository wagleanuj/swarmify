import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, type ReactNode, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";

import {
  blocked,
  choosable,
  decision,
  editable,
  GATE_KIND,
  gateArtifacts,
  gateProposal,
  type Choice,
  REJECT,
  type Proposal,
  sendBackTargets,
  staysOpen,
  topUpSuggestion,
  underReview,
  useGateForm,
} from "../api/answer";
import { api } from "../api/client";
import { labelOf, useConnectors } from "../api/connectors";
import { dropGate, needsYou, reloadGates, useOpenGates } from "../api/gates";
import type { ArtifactFile } from "../api/events";
import { abortAsk, type Gate, money, type Run, STATUS_LABEL, took } from "../api/runs";
import type { components } from "../api/schema";
import { useConfirm } from "../Dialog";
import { Icon } from "../Icon";
import { gatesRoute } from "../router";
import { piedPiper } from "../skin";
import { Loading, Skeleton } from "../Skeleton";
import { notify } from "../Toast";
import { ArtifactCards, ArtifactView } from "./Artifacts";
import { RunRef } from "./Runs";
import { PublishValue } from "./Connectors";
import { GateAsk } from "./GateAsk";
import { Menu } from "./Menu";

type World = components["schemas"]["World"];
type PublishPreview = components["schemas"]["PublishPreview"];
type StalledMemory = components["schemas"]["StalledMemory"];
type Notice = components["schemas"]["Notice"];

/** What `GateCard` hands back with an answer: `said` is the decision in plain words, for the
 * line that confirms it. */
export type Answer = (gate: Gate, send: () => Promise<string | null>, said?: string) => Promise<void>;

/** Below the web skin's 48rem breakpoint, where a gate's answer sits in a bar above the tab bar. */
const PHONE = "(max-width: 48rem)";
const onPhoneChange = (on: () => void) => {
  const m = window.matchMedia(PHONE);
  m.addEventListener("change", on);
  return () => m.removeEventListener("change", on);
};
const usePhone = () => useSyncExternalStore(onPhoneChange, () => window.matchMedia(PHONE).matches);

/** Whether the operator asked for less motion: then an answered card swaps out at once. */
const calm = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const ago = (at: number) => `${took(Math.max(0, (Date.now() - at) / 1000))} ago`;

type Answered = { gate: Gate; said: string; at: string };

/** The kinds as filter chips, short enough to fit a phone's row with their counts. */
const KIND_CHIP: [Gate["kind"], string][] = [
  ["approve", "Approve"],
  ["pick", "Pick"],
  ["confirm", "Confirm"],
  ["budget", "Budget"],
  ["skill", "Skill"],
];

/** Whole dollars without cents, for the budget's preset chips and its button. */
const usd = (n: number) => (Number.isInteger(n) ? `$${n}` : money(n));

export function Gates() {
  const search = gatesRoute.useSearch();
  const navigate = useNavigate();
  const phone = usePhone();
  const open = useOpenGates();
  const [worlds, setWorlds] = useState<World[]>([]);
  // Worlds whose memory stopped rebuilding: agents may then repeat what was already made.
  const [stalled, setStalled] = useState<StalledMemory[]>([]);
  // A refused sign-in, outcomes a connector could not read, a post still hidden a day on.
  const [notices, setNotices] = useState<Notice[]>([]);
  const [refused, setRefused] = useState<string | null>(null);
  // Answered this visit, newest first: they sort last in the queue, dimmed.
  const [answered, setAnswered] = useState<Answered[]>([]);
  // The card whose answer just landed shows its check for a beat; the next one then slides in.
  const [popping, setPopping] = useState<{ id: string; said: string } | null>(null);
  const [arrived, setArrived] = useState<string | null>(null);
  const [moving, setMoving] = useState<Run[] | null>(null);
  const deck = useRef<HTMLUListElement>(null);
  const swiped = useRef<number | undefined>(undefined);
  const error = refused ?? (open.failed ? "Could not load the gates. Is the API running?" : null);

  useEffect(() => {
    api
      .GET("/api/worlds")
      .then(({ data }) => setWorlds(data ?? []))
      .catch(() => undefined);
    api
      .GET("/api/graph/stalled")
      .then(({ data }) => setStalled(data ?? []))
      .catch(() => undefined);
    api
      .GET("/api/notices")
      .then(({ data }) => setNotices(data ?? []))
      .catch(() => undefined);
  }, []);

  const inWorld = (g: { world_id: string }) => !search.world || g.world_id === search.world;
  // A gate opened again after a send back is a new decision, so match the opening too.
  const isAnswered = (g: Gate) => answered.some((a) => a.gate.stage_id === g.stage_id && a.gate.opened_at === g.opened_at);
  const live = (open.gates ?? []).filter((g) => !isAnswered(g));
  const waiting = live.filter(inWorld);
  const elsewhere = live.length - waiting.length;
  const kinds = KIND_CHIP.flatMap(([k, label]) => {
    const n = waiting.filter((g) => g.kind === k).length;
    return n ? [{ kind: k, label, n }] : [];
  });
  const kind = kinds.some((k) => k.kind === search.kind) ? search.kind : undefined;
  const shown = kind ? waiting.filter((g) => g.kind === kind) : waiting;
  const at = Math.max(0, shown.findIndex((g) => g.stage_id === search.gate));
  const selected = shown[at];
  const done = answered.filter((a) => inWorld(a.gate));
  const clear = open.gates !== null && waiting.length === 0;

  const go = (gate: string | undefined, replace = false, k = kind) =>
    void navigate({ to: "/gates", search: { world: search.world, kind: k, gate }, replace });

  // An empty queue still leads somewhere: the runs moving in the worlds it covers.
  useEffect(() => {
    if (!clear) return;
    const ids = worlds.map((w) => w.id).filter((id) => !search.world || id === search.world);
    Promise.all(
      ids.map((id) => api.GET("/api/worlds/{world_id}/runs", { params: { path: { world_id: id } } }).then(({ data }) => data ?? [])),
    )
      .then((all) => setMoving(all.flat().filter((r) => r.status === "running" || r.status === "queued")))
      .catch(() => setMoving([]));
  }, [clear, worlds, search.world]);

  // On a phone the cards sit side by side; keep the open one in view when it changes elsewhere.
  useEffect(() => {
    const d = deck.current;
    const card = d?.children[at] as HTMLElement | undefined;
    if (!d || !card || !phone) return;
    const x = card.offsetLeft - (d.children[0] as HTMLElement).offsetLeft;
    if (Math.abs(d.scrollLeft - x) > 2) d.scrollTo({ left: x, behavior: "instant" });
  }, [at, phone, shown.length]);

  const answer: Answer = async (gate, send, said = "You answered it") => {
    setRefused(null);
    const why = await send();
    if (why) {
      setRefused(why);
      reloadGates();
      return;
    }
    if (staysOpen(gate)) {
      notify(`${said}.`, { tone: "ok" });
      reloadGates();
      return;
    }
    const rest = shown.filter((g) => g.stage_id !== gate.stage_id);
    const next = rest[Math.min(at, rest.length - 1)] ?? waiting.find((g) => g.stage_id !== gate.stage_id);
    setPopping({ id: gate.stage_id, said });
    if (!calm()) await new Promise((r) => setTimeout(r, 320));
    setPopping(null);
    dropGate(gate.stage_id);
    setAnswered((a) => [{ gate, said, at: new Date().toISOString() }, ...a]);
    setArrived(next?.stage_id ?? null);
    // The API cannot take an answer back, so the notice opens the run instead of undoing.
    notify(next ? `${said}. Next: ${GATE_KIND[next.kind]}` : `${said}. All clear.`, {
      tone: "ok",
      action: {
        label: "Open",
        run: () =>
          void navigate({
            to: "/worlds/$worldId/runs/$runId",
            params: { worldId: gate.world_id, runId: gate.run_id },
            search: { stage: gate.stage_id },
          }),
      },
    });
    go(next?.stage_id, false, next && rest.includes(next) ? kind : undefined);
  };

  const worldName = (id: string) => worlds.find((w) => w.id === id)?.name ?? id;
  const sub = waiting.length
    ? `Oldest waiting ${ago(Math.min(...waiting.map((g) => Date.parse(g.opened_at))))}. Answering one opens the next.`
    : "Every open gate from every world lands here.";
  const step = (by: number) => shown[at + by];
  const cards = phone ? shown : selected ? [selected] : [];

  return (
    <main className="needs-you">
      <header className="page needs-head">
        <div className="needs-title">
          <h1>Needs you</h1>
          {selected && (
            <span className="place" data-testid="place">
              <Link
                className="button icon"
                to="/gates"
                search={{ world: search.world, kind, gate: step(-1)?.stage_id }}
                aria-label="Previous decision"
                aria-disabled={!step(-1) || undefined}
                onClick={(e) => !step(-1) && e.preventDefault()}
              >
                <Icon name="back" />
              </Link>
              <span>
                {at + 1} of {shown.length}
              </span>
              <Link
                className="button icon"
                to="/gates"
                search={{ world: search.world, kind, gate: step(1)?.stage_id }}
                aria-label="Next decision"
                aria-disabled={!step(1) || undefined}
                onClick={(e) => !step(1) && e.preventDefault()}
              >
                <Icon name="next" />
              </Link>
            </span>
          )}
          <p className="sub">{sub}</p>
        </div>
        <label className="world-pick" data-on={search.world ? "" : undefined} title={search.world ? worldName(search.world) : "All worlds"}>
          <Icon name="world" />
          <span className="visually-hidden">World</span>
          <select
            value={search.world ?? ""}
            onChange={(e) => void navigate({ to: "/gates", search: e.target.value ? { world: e.target.value } : {} })}
          >
            <option value="">All worlds{open.gates ? ` (${live.length})` : ""}</option>
            {worlds.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
                {open.gates ? ` (${needsYou(live, w.id)})` : ""}
              </option>
            ))}
          </select>
        </label>
      </header>
      {error && <p role="alert">{error}</p>}
      {stalled.filter(inWorld).map((s) => (
        <p key={s.world_id} role="alert" className="notice" data-testid="stalled-memory">
          <span>
            {s.world_name}'s memory has not updated since {ago(Date.parse(s.since))}: its last {s.failures} rebuilds
            failed, the last with: {s.error.length > 200 ? `${s.error.slice(0, 200)}...` : s.error}. Agents may pitch
            what it already made.
          </span>
          <Link to="/worlds/$worldId/graph" params={{ worldId: s.world_id }}>
            Open its memory
          </Link>
        </p>
      ))}
      {notices
        // an account is shared by every world, so only a post's notice belongs to one
        .filter((n) => n.kind !== "visibility" || !n.world_id || inWorld({ world_id: n.world_id }))
        .map((n) => (
          <p key={`${n.kind} ${n.text}`} role="alert" className="notice" data-testid="notice">
            <span>{n.text}</span>
            {n.world_id && n.run_id ? (
              <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: n.world_id, runId: n.run_id }}>
                Open the run
              </Link>
            ) : n.world_id ? (
              <Link to="/worlds/$worldId/connectors" params={{ worldId: n.world_id }}>
                Open Connectors
              </Link>
            ) : null}
          </p>
        ))}
      {open.gates === null && !error && (
        <Loading className="inbox">
          <div className="queue q-skeleton">
            <Skeleton h={56} />
            <Skeleton h={56} />
          </div>
          <div className="gate g-skeleton">
            <Skeleton w="70%" h={24} />
            <Skeleton lines={2} />
            <Skeleton h={180} />
          </div>
        </Loading>
      )}
      {open.gates !== null && kinds.length > 0 && (
        <nav className="tabs chip-row kinds" aria-label="Kinds" data-testid="kinds">
          <Link className="tab" to="/gates" search={{ world: search.world }} aria-current={!kind ? "page" : undefined}>
            All <span className="num">{waiting.length}</span>
          </Link>
          {kinds.map((k) => (
            <Link
              key={k.kind}
              className="tab"
              to="/gates"
              search={{ world: search.world, kind: k.kind }}
              aria-current={kind === k.kind ? "page" : undefined}
            >
              {k.label} <span className="num">{k.n}</span>
            </Link>
          ))}
        </nav>
      )}
      {open.gates !== null && (
        <div className="inbox">
          {!phone && (
            <nav className="queue" aria-label="Waiting decisions" data-testid="queue">
              {shown.map((g) => (
                <Link
                  key={g.stage_id}
                  className="q-item"
                  to="/gates"
                  search={{ world: search.world, kind, gate: g.stage_id }}
                  aria-current={g === selected ? "true" : undefined}
                >
                  <span className="q-line">
                    <b>{GATE_KIND[g.kind]}</b>
                    <span className="age">{took(Math.max(0, (Date.now() - Date.parse(g.opened_at)) / 1000))}</span>
                  </span>
                  <span className="muted">
                    {g.run_name} · {worldName(g.world_id)}
                  </span>
                </Link>
              ))}
              {done.map((a) => (
                <Link
                  key={`${a.gate.stage_id}-${a.at}`}
                  className="q-item done"
                  to="/worlds/$worldId/runs/$runId"
                  params={{ worldId: a.gate.world_id, runId: a.gate.run_id }}
                >
                  <span className="q-line">
                    <b>{GATE_KIND[a.gate.kind]}</b>
                    <span className="age">
                      <Icon name="check" /> Answered
                    </span>
                  </span>
                  <span className="muted">
                    {a.gate.run_name} · {worldName(a.gate.world_id)}
                  </span>
                </Link>
              ))}
              {waiting.length === 0 && done.length === 0 && <p className="faint">No decisions here.</p>}
            </nav>
          )}
          <div className="inbox-main">
            {cards.length > 0 ? (
              <ul
                ref={deck}
                className="gate-list deck"
                aria-label="Decisions"
                onScroll={(e) => {
                  // A swipe that came to rest on another card opens it; prev and next do the same.
                  if (!phone) return;
                  const d = e.currentTarget;
                  clearTimeout(swiped.current);
                  swiped.current = window.setTimeout(() => {
                    const first = (d.children[0] as HTMLElement).offsetLeft;
                    const near = [...d.children].reduce(
                      (best, c, i) =>
                        Math.abs((c as HTMLElement).offsetLeft - first - d.scrollLeft) <
                        Math.abs((d.children[best] as HTMLElement).offsetLeft - first - d.scrollLeft)
                          ? i
                          : best,
                      0,
                    );
                    const g = shown[near];
                    if (g && g !== selected) {
                      setArrived(null);
                      go(g.stage_id, true);
                    }
                  }, 120);
                }}
              >
                {cards.map((g) => (
                  <GateCard
                    key={`${g.stage_id}/${g.opened_at}`}
                    gate={g}
                    world={worldName(g.world_id)}
                    onAnswer={answer}
                    inactive={g !== selected}
                    arriving={g.stage_id === arrived}
                    done={popping?.id === g.stage_id ? popping.said : undefined}
                  />
                ))}
              </ul>
            ) : (
              <section className="empty all-clear" data-testid="no-gates">
                {search.world && elsewhere > 0 ? (
                  <>
                    <h2>Nothing waiting in {worldName(search.world)}</h2>
                    <p>
                      {elsewhere} {elsewhere === 1 ? "decision waits" : "decisions wait"} in other worlds.{" "}
                      <Link to="/gates">Show every world</Link>
                    </p>
                  </>
                ) : (
                  <>
                    <h2>
                      <Icon name="check" size={20} className={done.length ? "i-pop" : undefined} /> All clear
                    </h2>
                    {done.length > 0 && (
                      <p>
                        You answered {done.length === 1 ? "one decision" : `${done.length} decisions`} this visit.
                      </p>
                    )}
                  </>
                )}
                <p className="faint">
                  When a run reaches a gate it lands here, pausing until you pick, approve or confirm, and so does a
                  run whose budget ran out.
                </p>
                <h3>Runs still moving</h3>
                {moving === null ? (
                  <Loading className="moving">
                    <Skeleton h={42} />
                    <Skeleton h={42} />
                  </Loading>
                ) : moving.length === 0 ? (
                  <p className="faint">No runs are moving.</p>
                ) : (
                  <ul className="moving" data-testid="moving">
                    {moving.map((r) => (
                      <li key={r.id}>
                        <span className={r.status === "running" ? "dot live" : "dot"} />
                        <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: r.world_id, runId: r.id }}>
                          {r.name}
                        </Link>
                        <span className="faint">
                          {worldName(r.world_id)} · {STATUS_LABEL[r.status]}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </main>
  );
}

export function GateCard({
  gate,
  world,
  onAnswer,
  bare = false,
  inactive = false,
  arriving = false,
  done,
}: {
  gate: Gate;
  world: string;
  /** On the run page, which already names the run and the step. */
  bare?: boolean;
  onAnswer: Answer;
  /** A phone's neighbouring card in the swipe deck: shown, not answerable until it is open. */
  inactive?: boolean;
  /** The next card after an answer: it slides in from the right. */
  arriving?: boolean;
  /** The answer just taken, in words: the bar shows it with a check for a beat. */
  done?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [confirm, dialog] = useConfirm();
  const phone = usePhone();
  const formId = useId();
  // The pick so far (a card's path, or the typed name), so a phone's primary can name it.
  const [chosen, setChosen] = useState("");
  // The budget's top-up, from a preset chip or typed.
  const [add, setAdd] = useState(() => topUpSuggestion(gate));
  // Ask this run mounts the first time its disclosure opens, then stays with its turns.
  const [asking, setAsking] = useState(false);
  // The earlier step a Send back menu item chose; the form then submits with it.
  const sendBack = useRef<string | null>(null);
  // The gate's form, and whether an editable field of the post differs from what the gate shows.
  const { form, changed, edited, fill } = useGateForm(gate);
  // What the earlier stages made: the cards a pick chooses from, the piece an approve reviews
  // (stories 53 and 54). null until loaded, so the pick's fallback field does not flash.
  const [artifacts, setArtifacts] = useState<ArtifactFile[] | null>(null);
  const shows = gate.kind === "pick" || gate.kind === "approve";

  useEffect(() => {
    if (!shows) return;
    gateArtifacts(gate)
      .then(setArtifacts)
      .catch(() => setArtifacts([]));
  }, [shows, gate.run_id, gate.stage_index, gate.send_backs_left]);

  const cards = gate.kind === "pick" && artifacts ? choosable(artifacts) : [];
  const reviewed = gate.kind === "approve" && artifacts ? underReview(artifacts) : [];
  const card = cards.findIndex((c) => c.path === chosen);
  const pickLabel = !phone
    ? "Continue with this pick"
    : card >= 0
      ? `Continue with #${String(cards[card]!.fields.rank ?? card + 1)}`
      : chosen
        ? "Continue with this pick"
        : "Continue";
  const spent = gate.spent_usd ?? 0;
  const budget = gate.budget_usd ?? 0;
  const presets = [...new Set([topUpSuggestion(gate), 1, 5, 10])].sort((a, b) => a - b);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fields = new FormData(e.currentTarget);
    const target = sendBack.current;
    sendBack.current = null;
    const picked = cards.find((c) => c.path === fields.get("artifact"));
    const choice: Choice =
      gate.kind === "budget"
        ? { kind: "budget", add: Number(fields.get("add")) }
        : target
          ? { kind: "send_back", to: target }
          : gate.kind === "pick"
            ? picked
              ? { kind: "pick", choice: picked.title, artifact: picked.path }
              : { kind: "pick", choice: String(fields.get("choice") ?? "").trim() }
            : { kind: gate.kind === "confirm" ? "confirm" : "approve" };
    const edits = Object.fromEntries(
      [...fields].flatMap(([k, v]) => (k.startsWith("edit:") ? [[k.slice(5), String(v)]] : [])),
    );
    const { send, said } = decision(gate, choice, { note: String(fields.get("note") ?? "").trim(), edits, reviewed });
    setBusy(true);
    await onAnswer(gate, send, said);
    setBusy(false);
  }

  async function abort() {
    if (!(await confirm(abortAsk(gate.run_name, gate.spent_usd, gate.budget_usd)))) return;
    setBusy(true);
    const { send, said } = decision(gate, { kind: "abort" });
    await onAnswer(gate, send, said);
    setBusy(false);
  }

  // The note and Ask this run, folded under the work so the decision stays in the first screen.
  // The note belongs to the gate's form through its `form` attribute.
  const more = (gate.kind !== "budget" || !bare) && (
    <details className="gate-more" onToggle={(e) => e.currentTarget.open && setAsking(true)}>
      <summary>{gate.kind === "budget" ? "Ask before you answer" : bare ? "Add a note" : "Add a note or ask"}</summary>
      {gate.kind !== "budget" && (
        <label>
          {gate.kind === "approve"
            ? "Note (sent back with it, or kept with the approval)"
            : gate.kind === "skill"
              ? "Note (kept with the decision; on a rejection, say why)"
              : "Note (optional)"}
          <textarea name="note" rows={2} form={formId} />
        </label>
      )}
      {/* The run page has its own Ask this run. */}
      {!bare && asking && <GateAsk gate={gate} onFill={fill} />}
    </details>
  );
  const answeredBar = done && (
    <div className="row gate-actions action-bar gate-done" role="status">
      <Icon name="check" size={20} className="i-pop" />
      <span>{done}</span>
    </div>
  );

  return (
    <li
      className={arriving ? "gate arrive" : "gate"}
      data-kind={gate.kind}
      data-testid={`gate-${gate.run_id}`}
      aria-current={inactive ? undefined : "true"}
      inert={inactive || undefined}
    >
      {dialog}
      {!bare && (
        <p className="gate-ctx">
          <span>{world}</span>
          <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: gate.world_id, runId: gate.run_id }}>
            {gate.run_name}
          </Link>
          <span>
            stage {gate.stage_index + 1}, {gate.stage_name}
          </span>
          <span>waiting {took(Math.max(0, (Date.now() - Date.parse(gate.opened_at)) / 1000))}</span>
          {gate.parent && <RunRef label="child of" run={gate.parent} testId="child-of" />}
        </p>
      )}
      <p className="ask">{gate.ask}</p>
      {/* Holds the evidence's place while it loads, so the form below does not jump down. */}
      {shows && artifacts === null && (
        <Loading>
          <Skeleton h={240} />
        </Loading>
      )}
      {reviewed.length > 0 && <ArtifactView artifacts={reviewed} worldId={gate.world_id} />}
      {gate.kind === "approve" && artifacts && reviewed.length === 0 && (
        <p className="faint">The earlier stages left nothing to show here; their notes are on the run's timeline.</p>
      )}
      {gate.kind === "skill" ? (
        <SkillGate gate={gate} formId={formId} more={more} done={answeredBar} onAnswer={onAnswer} />
      ) : (
        <>
          <form
            id={formId}
            ref={form}
            onSubmit={submit}
            onChange={(e) => {
              const f = e.target as unknown as HTMLInputElement;
              if (f.name === "artifact" || f.name === "choice") setChosen(f.value.trim());
              if (f.name.startsWith("edit:")) edited();
            }}
            className="gate-form"
            aria-label={`Answer ${gate.stage_name}`}
          >
            {gate.publish && <WhatGoesOut key={JSON.stringify(gate.publish)} preview={gate.publish} worldId={gate.world_id} />}
            {gate.kind === "budget" && (
              <div className="top-up">
                <p className="spent" data-testid="budget-spent">
                  {piedPiper() ? "Russ has" : "This run has"} spent <b>{money(spent)}</b> of {money(budget)}. Adding{" "}
                  {usd(add || 0)} makes it {money(Math.round((budget + (add || 0)) * 100) / 100)}; the stopped agents resume where they
                  stopped.
                </p>
                <div className="presets" role="group" aria-label="Add">
                  {presets.map((p) => (
                    <button key={p} type="button" className="chip-button" aria-pressed={add === p} onClick={() => setAdd(p)}>
                      +{usd(p)}
                    </button>
                  ))}
                </div>
                <label>
                  Or another amount (US dollars)
                  <input
                    name="add"
                    type="number"
                    inputMode="decimal"
                    min="0.01"
                    step="0.01"
                    required
                    value={add || ""}
                    onChange={(e) => setAdd(Number(e.target.value))}
                  />
                </label>
                <p className="faint">
                  Abort ends it instead: finished stages keep their files, and the {money(spent)} spent stays spent.
                </p>
              </div>
            )}
            {gate.kind === "pick" && cards.length > 0 && <ArtifactCards cards={cards} worldId={gate.world_id} />}
            {gate.kind === "pick" && artifacts && cards.length === 0 && (
              <>
                <label>
                  Your pick
                  <input name="choice" required autoComplete="off" placeholder="idea 1" />
                </label>
                <span className="faint">
                  No cards reached this gate, so name your pick; it is kept word for word.
                </span>
              </>
            )}
          </form>
          {more}
          {answeredBar || (
            <div className="row gate-actions action-bar">
              {gate.kind === "budget" && (
                <>
                  <button type="submit" form={formId} className="primary" disabled={busy || !(add > 0)}>
                    Add {usd(add || 0)} and go on
                  </button>
                  <button type="button" className="danger" onClick={abort} disabled={busy}>
                    Abort the run
                  </button>
                </>
              )}
              {gate.kind === "pick" && (
                <>
                  <button type="submit" form={formId} className="primary" disabled={busy || artifacts === null || (phone && !chosen)}>
                    {pickLabel}
                  </button>
                  {cards.length > 0 && <span className="faint">The ones you pass on stay in the library.</span>}
                </>
              )}
              {gate.kind === "confirm" && (
                <>
                  <button type="submit" form={formId} className="primary" disabled={busy || blocked(gate, changed)}>
                    {gate.publish ? "Confirm and post" : "Confirm"}
                  </button>
                  <span className="faint">
                    {gate.publish
                      ? "Posts exactly what is above. Nothing goes out until you confirm."
                      : "Nothing goes out until you confirm."}
                  </span>
                </>
              )}
              {gate.kind === "approve" && (
                <>
                  {/* Waits for what it reviews, so an approval of one piece names it in the record. */}
                  <button
                    type="submit"
                    form={formId}
                    className="primary"
                    disabled={busy || artifacts === null || blocked(gate, changed)}
                  >
                    Approve
                  </button>
                  {sendBackTargets(gate).length > 0 ? (
                    <>
                      <Menu
                        label="Send back to..."
                        button={phone ? "Send back…" : "Send back to..."}
                        items={sendBackTargets(gate).map((name) => ({
                          label: name,
                          note: `Redo ${name} with your notes`,
                          disabled: busy,
                          onSelect: () => {
                            sendBack.current = name;
                            form.current?.requestSubmit();
                          },
                        }))}
                      />
                      <span className="faint">
                        {gate.send_backs_left} of {gate.max_send_backs} revisions left
                      </span>
                    </>
                  ) : (
                    <span className="faint">
                      No revisions left at this gate. Approve, or abort the run from its timeline.
                    </span>
                  )}
                </>
              )}
            </div>
          )}
        </>
      )}
    </li>
  );
}

/** The retro's proposal: at most one skill change to approve as is, edited, or reject, and the
 * workflow change it drew from a recurring intervention (ADR 0008). */
function SkillGate({
  gate,
  formId,
  more,
  done,
  onAnswer,
}: {
  gate: Gate;
  /** The form the card's note belongs to, and the buttons below it submit. */
  formId: string;
  /** The card's note and ask disclosure, between the change and the decision. */
  more: ReactNode;
  /** The answered bar, in place of the decision for a beat. */
  done: ReactNode;
  onAnswer: Answer;
}) {
  const [proposal, setProposal] = useState<Proposal | null | undefined>(undefined);
  const [content, setContent] = useState("");
  const [applyWorkflow, setApplyWorkflow] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirm, dialog] = useConfirm();

  useEffect(() => {
    gateProposal(gate)
      .then((data) => {
        setProposal(data);
        setContent(data?.skill?.content ?? "");
      })
      .catch(() => setProposal(null));
  }, [gate.run_id]);

  async function answer(e: FormEvent<HTMLFormElement>, approve: boolean) {
    e.preventDefault();
    if (!proposal) return;
    const note = String(new FormData(e.currentTarget).get("note") ?? "").trim();
    if (!approve && !note && !(await confirm(REJECT))) return;
    const { send, said } = decision(gate, { kind: "skill", proposal, approve, content, applyWorkflow }, { note });
    setBusy(true);
    await onAnswer(gate, send, said);
    setBusy(false);
  }

  if (proposal === undefined) {
    return (
      <Loading className="proposal">
        <Skeleton w="50%" />
        <Skeleton lines={2} />
        <Skeleton h={160} />
      </Loading>
    );
  }
  if (proposal === null) {
    return <p className="faint">The proposal could not be loaded; the run keeps waiting.</p>;
  }
  const skill = proposal.skill;
  return (
    <>
    {dialog}
    <form id={formId} className="gate-form proposal" aria-label={`Answer ${gate.stage_name}`} onSubmit={(e) => void answer(e, true)}>
      {skill && (
        <section data-testid="skill-change">
          <b>
            {skill.action === "new" && `New skill ${skill.name}`}
            {skill.action === "changed" && `Change skill ${skill.name}`}
            {skill.action === "retired" && `Retire skill ${skill.name}`}
          </b>
          <p>{skill.summary}</p>
          <ul aria-label="Evidence">
            {(skill.evidence ?? []).map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
          {skill.action !== "retired" && (
            <details>
              <summary>Read or edit SKILL.md before approving</summary>
              <textarea aria-label="SKILL.md (edit before approving)" value={content} rows={8} onChange={(e) => setContent(e.target.value)} />
            </details>
          )}
        </section>
      )}
      {proposal.workflow && (
        <section data-testid="workflow-change">
          <b>Workflow change: {proposal.workflow.summary}</b>
          <ul>
            {(proposal.workflow.evidence ?? []).map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
          <details>
            <summary>Read the new workflow</summary>
            <pre>{proposal.workflow.yaml}</pre>
          </details>
          <label className="check">
            <input type="checkbox" checked={applyWorkflow} onChange={(e) => setApplyWorkflow(e.target.checked)} />
            Save it as the workflow's next version when approving
          </label>
        </section>
      )}
    </form>
    {more}
    {done || (
      <div className="row gate-actions action-bar">
        <button type="submit" form={formId} className="primary" disabled={busy}>
          Approve
        </button>
        <RejectButton formId={formId} busy={busy} onReject={(e) => void answer(e, false)} />
        <span className="faint">
          {skill ? "Approving writes the skill as a new version every later stage loads." : "Nothing changes until you approve."}
        </span>
      </div>
    )}
    </>
  );
}

/** A reject button that submits the surrounding form with the rejection intent. */
function RejectButton({ formId, busy, onReject }: { formId: string; busy: boolean; onReject: (e: FormEvent<HTMLFormElement>) => void }) {
  return (
    <button
      type="button"
      form={formId}
      className="danger"
      disabled={busy}
      onClick={(e) => {
        const form = (e.currentTarget as HTMLButtonElement).form;
        if (!form) return;
        onReject({ preventDefault: () => undefined, currentTarget: form } as unknown as FormEvent<HTMLFormElement>);
      }}
    >
      Reject
    </button>
  );
}

/** What the publish stage after this gate sends out: exactly these values, as its connector
 * describes them (ADR 0007). Fields it marks editable are fields of the gate's form. */
function WhatGoesOut({ preview, worldId }: { preview: PublishPreview; worldId: string }) {
  const id = useId();
  const connectors = useConnectors(worldId);
  const problems = preview.problems ?? [];
  return (
    <section className="publish-preview" aria-label="What goes out">
      <dl>
        <dt>Where</dt>
        <dd>{labelOf(connectors, preview.connector)}</dd>
        {(preview.fields ?? []).map((f) => (
          <div key={f.key} className="publish-field">
            <dt>{editable(f) ? <label htmlFor={`${id}-${f.key}`}>{f.label}</label> : f.label}</dt>
            <dd>
              {editable(f) ? (
                <>
                  <textarea
                    id={`${id}-${f.key}`}
                    name={`edit:${f.key}`}
                    rows={String(f.value ?? "").length > 60 || String(f.value ?? "").includes("\n") ? 3 : 1}
                    defaultValue={String(f.value ?? "")}
                  />
                  {f.hint && <span className="faint">{f.hint}</span>}
                </>
              ) : (
                <PublishValue value={f.value} hint={f.hint} />
              )}
            </dd>
          </div>
        ))}
      </dl>
      {problems.length > 0 && (
        <div className="publish-problems" role="alert">
          <p>This will not post:</p>
          <ul>
            {problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
          <p>
            {(preview.fields ?? []).some(editable)
              ? "Edit the fields above to fix it, then confirm. Or abort the run from its timeline, fix the publish stage, and start a new run."
              : "Abort the run from its timeline, fix the publish stage, and start a new run."}
          </p>
        </div>
      )}
    </section>
  );
}
