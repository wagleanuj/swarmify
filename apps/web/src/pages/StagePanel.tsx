import { Link } from "@tanstack/react-router";
import { type ReactNode, useEffect, useState } from "react";

import { api } from "../api/client";
import type { Agent, ArtifactFile, LogEntry, Verdict } from "../api/events";
import { type Call, clock, decisionLine, PUBLISHERS, type Run } from "../api/runs";
import type { components } from "../api/schema";
import type { Task } from "../api/tasks";
import { AgentCards, describe, LiveLog, usd } from "./AgentLog";
import { AUDIO, fileUrl, IMAGE, Markdown, RECORD, rawUrl, summary, VIDEO } from "./Artifacts";
import { LABEL } from "./Connectors";
import { StageOutput } from "./StageOutput";
import { Whiteboard } from "./Whiteboard";

type Stage = Run["stages"][number];
type Proposal = components["schemas"]["Proposal"];

/** Seconds a stage took, or has taken so far. */
export function stageSeconds(s: Stage, now = Date.now()): number | null {
  if (!s.started_at) return null;
  const end = s.ended_at ? Date.parse(s.ended_at) : now;
  return Math.max(0, (end - Date.parse(s.started_at)) / 1000);
}

/** A step's state in a word or its duration: "2m 14s", "running", "waiting". */
export function stepMeta(s: Stage, queued = false): string {
  const took = stageSeconds(s);
  switch (s.status) {
    case "done":
      return took === null ? "done" : clock(took);
    case "running":
      return "running";
    case "waiting":
      return "waiting";
    case "failed":
      return took === null ? "failed" : `failed after ${clock(took)}`;
    case "skipped":
      return "skipped";
    default:
      return queued ? "starting" : "not yet";
  }
}

/** What kind of stage this is, as the panel names it. */
export function kindOf(s: Stage): string {
  if (s.type !== "connector") return s.type;
  if (s.calls === "remotion_build") return "build";
  if (s.calls === "remotion_render") return "render";
  if (s.calls === "speech") return "speech";
  if (PUBLISHERS.has(s.calls ?? "")) return "publish";
  return "connector";
}

const isPublish = (s: Stage, made: ArtifactFile[]) => kindOf(s) === "publish" || made.some((a) => a.type === "publish");

/** The run's work: what its stages made, not what it wrote about itself or what it posted. */
const work = (artifacts: ArtifactFile[]) =>
  artifacts.filter((a) => !RECORD.has(a.type) && a.type !== "publish" && !a.fields.deleted);

/** The stage whose output is the run's result: the latest stage that made anything. */
export function resultStage(run: Run, artifacts: ArtifactFile[]): Stage | undefined {
  const made = new Set(work(artifacts).map((a) => a.stage));
  return run.stages.filter((s) => made.has(s.id)).at(-1);
}

/** The run's primary output, from the stage that made it: a picture or sound over a list over a
 * document over a plain file. */
function primary(artifacts: ArtifactFile[]): ArtifactFile[] {
  const media = artifacts.filter((a) => a.type === "video" || VIDEO.test(a.name) || IMAGE.test(a.name) || AUDIO.test(a.name));
  if (media.length) return [media.at(-1)!];
  const items = artifacts.filter((a) => a.type !== "file");
  if (items.length > 1) return items;
  return [items[0] ?? artifacts.at(-1)!];
}

/** A file's text without the YAML frontmatter on top. */
const body = (text: string) => text.replace(/^---\n[\s\S]*?\n---\n/, "");

/** The first lines of a document, enough to recognise it. */
function firstLines(text: string, lines = 8): string {
  return body(text).split("\n").filter((l) => l.trim()).slice(0, lines).join("\n\n");
}

/** One artifact, shown in place: a player, the picture, the first lines of a document. */
export function Made({ a, worldId }: { a: ArtifactFile; worldId: string }) {
  const url = rawUrl(worldId, a);
  if (a.type === "video" || VIDEO.test(a.name)) {
    return <video className="player" controls preload="metadata" src={url} data-testid="player" />;
  }
  if (AUDIO.test(a.name)) return <audio controls preload="metadata" src={url} data-testid="audio" />;
  if (IMAGE.test(a.name)) return <img className="player" src={url} alt={a.title} />;
  if (a.name === "timings.json") return <SpeechTakes timings={a} worldId={worldId} />;
  if (/\.(md|txt)$/i.test(a.name)) {
    return (
      <div className="document" data-testid="document">
        <Markdown text={firstLines(a.body)} />
        <a href={url} target="_blank" rel="noreferrer" className="button">
          Open
        </a>
      </div>
    );
  }
  return (
    <a href={url} target="_blank" rel="noreferrer">
      Open {a.name}
    </a>
  );
}

/** Typed items (ideas, candidates) as cards: title and hook or summary. */
function ItemCards({ items }: { items: ArtifactFile[] }) {
  const sorted = [...items].sort((a, b) => Number(a.fields.rank ?? 1e9) - Number(b.fields.rank ?? 1e9));
  return (
    <ul className="cards" data-testid="items">
      {sorted.map((a) => (
        <li key={a.path} className="card" data-testid="item">
          <span className="faint">
            {a.type}
            {a.fields.rank != null ? ` · #${String(a.fields.rank)} of ${items.length}` : ""}
          </span>
          <b>{a.title}</b>
          <span>{typeof a.fields.hook === "string" ? a.fields.hook : summary(a.body)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Part 4: the run's latest primary output, in place, above every control. */
export function Result({ run, artifacts, worldId }: { run: Run; artifacts: ArtifactFile[]; worldId: string }) {
  const from = resultStage(run, artifacts);
  const shown = from ? primary(work(artifacts).filter((a) => a.stage === from.id)) : [];
  const one = shown.length === 1 ? shown[0]! : null;
  return (
    <section className={`result${one ? " result-one" : ""}`} aria-label="Result" data-testid="result">
      {!from ? (
        <p className="faint">Nothing made yet. What the run makes shows here as soon as a stage makes it.</p>
      ) : one ? (
        <>
          <Made a={one} worldId={worldId} />
          <dl className="facts">
            <dt>What</dt>
            <dd>{one.title}</dd>
            <dt>Made by</dt>
            <dd>
              {from.name}, step {from.index + 1} of {run.stages.length}
            </dd>
            {!Number.isNaN(Date.parse(one.created)) && (
              <>
                <dt>When</dt>
                <dd>{new Date(one.created).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</dd>
              </>
            )}
            <dt>File</dt>
            <dd>
              <a href={rawUrl(worldId, one)} target="_blank" rel="noreferrer">
                {one.name}
              </a>
            </dd>
          </dl>
        </>
      ) : (
        <>
          <p className="faint">
            {shown.length} {shown[0]!.type}s from {from.name}, step {from.index + 1} of {run.stages.length}
          </p>
          <ItemCards items={shown} />
        </>
      )}
    </section>
  );
}

type Take = {
  scene: string;
  file: string;
  duration: number;
  lines?: { text: string; joined_next?: boolean }[];
};

type Timings = {
  engine: string;
  takes: Take[];
  /** The voice that spoke, a catalog name or an engine's own; null for the engine's default. */
  voice: string | null;
  /** The voice the stage or the script asked for, when it differs from the one that spoke. */
  chosen: string | null;
  warnings: string[];
};

/** The speech connector's timings.json: one take per scene, voiced by one engine in one voice. */
function takes(a: ArtifactFile): Timings | null {
  try {
    const t = JSON.parse(a.body) as Partial<Timings>;
    return {
      engine: t.engine ?? "an unnamed engine",
      takes: t.takes ?? [],
      voice: t.voice ?? null,
      chosen: t.chosen ?? null,
      warnings: t.warnings ?? [],
    };
  } catch {
    return null;
  }
}

/** An engine as its address names it, without the scheme. */
const engineName = (engine: string) => engine.replace(/^https?:\/\//, "");

/** One row per scene: its take, its length, the engine, and a warning where a join has no
 * silence to cut at. */
export function SpeechTakes({ timings, worldId }: { timings: ArtifactFile; worldId: string }) {
  const t = takes(timings);
  if (!t) return <p className="faint">timings.json could not be read.</p>;
  const total = t.takes.reduce((sum, k) => sum + k.duration, 0);
  const folder = timings.path.split("/").slice(0, -1).join("/");
  return (
    <div className="takes" data-testid="speech">
      <p>
        <b>
          {t.takes.length} {t.takes.length === 1 ? "scene" : "scenes"} · {total.toFixed(1)} s of narration
        </b>{" "}
        · <span data-testid="voice">{t.voice ? `voiced by ${t.voice}` : "the engine's default voice"}</span>
        {t.chosen && t.chosen !== t.voice && (
          <span className="faint"> (chosen {t.chosen}, which {engineName(t.engine)} cannot speak)</span>
        )}
      </p>
      {t.warnings.map((w) => (
        <p key={w} className="warn" role="note">
          {w}
        </p>
      ))}
      <ul className="take-list">
        {t.takes.map((k) => {
          const joined = (k.lines ?? []).flatMap((l, i) => (l.joined_next ? [i + 1] : []));
          return (
            <li key={k.scene} data-testid="take">
              <b>{k.scene}</b>
              <audio controls preload="none" src={fileUrl(worldId, `${folder}/${k.file}`)} />
              <span className="num">{k.duration.toFixed(1)} s</span>
              <span className="faint">{engineName(t.engine)}</span>
              {joined.map((i) => (
                <span key={i} className="warn" role="note">
                  Line {i} runs into line {i + 1} with no silence to cut at.
                </span>
              ))}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** "Rendered 412 of 870 frames" from the render's progress message. */
function frames(call: Call | null | undefined): { label: string; value: number } | null {
  if (!call) return null;
  const m = /(rendered|encoded) (\d+)\/(\d+) frames/i.exec(call.message ?? "");
  const value = call.status === "done" ? 1 : (call.progress ?? 0);
  if (call.status === "done") return { label: "Rendered every frame", value };
  if (!m) return { label: call.message ? `${call.message[0]!.toUpperCase()}${call.message.slice(1)}` : "Starting the render", value };
  const word = m[1]!.toLowerCase() === "rendered" ? "Rendered" : "Encoded";
  return { label: `${word} ${m[2]} of ${m[3]} frames`, value };
}

function RenderProgress({ call }: { call: Call | null | undefined }) {
  const f = frames(call);
  if (!f) return <p className="faint">The render starts once the stage before it is done.</p>;
  return (
    <div className="progress" data-testid="render-progress">
      <span>{f.label}</span>
      <meter min={0} max={1} value={f.value} aria-label="Render progress" />
    </div>
  );
}

/** The video a render made, if any. */
function Video({ made, worldId, shown }: { made: ArtifactFile[]; worldId: string; shown?: boolean }) {
  const v = made.find((a) => a.type === "video" || VIDEO.test(a.name));
  if (!v) return null;
  if (shown) return <p className="faint">It made {v.name}, the result shown above.</p>;
  return <Made a={v} worldId={worldId} />;
}

function BuildPanel(props: {
  call: Call | null | undefined;
  log: LogEntry[];
  agents: Agent[];
  made: ArtifactFile[];
  worldId: string;
  shown?: boolean;
}) {
  const { call, log, agents, made, worldId, shown } = props;
  const builder = agents.find((a) => a.name === "builder");
  const steps = log.filter((e) => e.agent === "builder" && e.kind !== "session");
  const project = typeof call?.request.project === "string" ? call.request.project : null;
  const [copied, setCopied] = useState(false);
  return (
    <>
      <section className="phase" aria-label="Writing the project" data-testid="build-log">
        <div className="row">
          <h3>Writing the project</h3>
          {builder && <span className="faint">{usd(builder.cost_usd)}</span>}
        </div>
        {steps.length === 0 ? (
          <p className="faint">The builder has not started. It writes the project, then the render starts.</p>
        ) : (
          <ol className="log">
            {steps.map((e) => (
              <li key={e.seq} className={`log-${e.kind}`}>
                {describe(e)}
              </li>
            ))}
          </ol>
        )}
      </section>
      <section className="phase" aria-label="Rendering">
        <h3>Rendering</h3>
        <RenderProgress call={call} />
      </section>
      <Video made={made} worldId={worldId} shown={shown} />
      {project && (
        <p className="project">
          Project folder <code data-testid="project">{project}</code>{" "}
          <button
            type="button"
            className="link"
            onClick={() => void navigator.clipboard?.writeText(project).then(() => setCopied(true))}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </p>
      )}
    </>
  );
}

function PublishPanel({ made, call }: { made: ArtifactFile[]; call: Call | null | undefined }) {
  const post = made.find((a) => a.type === "publish");
  if (!post) {
    return <p className="faint">Nothing has gone out from this stage. It posts what the gate before it confirmed.</p>;
  }
  const f = post.fields;
  const links = Object.values((f.links as Record<string, string> | undefined) ?? {});
  const where = LABEL[String(f.connector)] ?? String(f.connector ?? call?.connector ?? "");
  return (
    <div className="published" data-testid="publish">
      <dl>
        <dt>Title</dt>
        <dd>{String(f.title ?? post.title)}</dd>
        <dt>Where</dt>
        <dd>{where}</dd>
        <dt>Visibility</dt>
        <dd>{String(f.visibility ?? "")}</dd>
        <dt>When</dt>
        <dd>{f.schedule ? new Date(String(f.schedule)).toLocaleString() : new Date(String(f.created ?? post.created)).toLocaleString()}</dd>
        {typeof f.views === "number" && (
          <>
            <dt>Views</dt>
            <dd data-testid="views">
              {f.views.toLocaleString("en")} as of {String(f.views_at ?? "").slice(0, 10)}
            </dd>
          </>
        )}
      </dl>
      {links[0] && (
        <a href={links[0]} target="_blank" rel="noreferrer" className="button">
          Open the post
        </a>
      )}
    </div>
  );
}

function RetroPanel({ run, stage }: { run: Run; stage: Stage }) {
  const [proposal, setProposal] = useState<Proposal | null | undefined>(undefined);
  useEffect(() => {
    api
      .GET("/api/worlds/{world_id}/proposals", { params: { path: { world_id: run.world_id } } })
      .then(({ data }) => setProposal((data ?? []).filter((p) => p.run_id === run.id).at(-1) ?? null))
      .catch(() => setProposal(null));
  }, [run.world_id, run.id, stage.status]);
  if (proposal === undefined) return <p role="status">Loading the proposal...</p>;
  if (proposal === null) {
    return (
      <p className="faint" data-testid="retro">
        {stage.status === "done" ? "The retro read this run and proposed no change." : "The retro reads the run once every other stage is done."}
      </p>
    );
  }
  const outcome = { pending: "Waiting for you to approve or reject it.", approved: "You approved it.", rejected: "You rejected it." }[
    proposal.status
  ];
  return (
    <div data-testid="retro">
      {proposal.skill && (
        <p>
          <b>
            {proposal.skill.action === "new" ? "New skill" : proposal.skill.action === "changed" ? "Change skill" : "Retire skill"}{" "}
            {proposal.skill.name}
          </b>
          : {proposal.skill.summary}
        </p>
      )}
      {proposal.workflow && <p>Workflow change: {proposal.workflow.summary}</p>}
      <p>
        {outcome}
        {proposal.note ? ` Your note: ${proposal.note}` : ""}
      </p>
    </div>
  );
}

function GatePanel({ run, stage, open }: { run: Run; stage: Stage; open: boolean }) {
  const decisions = (run.decisions ?? []).filter((d) => d.stage_index === stage.index);
  if (open) return null; // the open gate leads the panel, its question included
  return (
    <div data-testid="gate-panel">
      {stage.ask && <p className="ask">{stage.ask}</p>}
      {decisions.map((d) => (
        <p key={d.id} data-testid="decision">
          {decisionLine(d)}
        </p>
      ))}
      {decisions.length === 0 && stage.status !== "waiting" && <p className="faint">Not reached yet.</p>}
    </div>
  );
}

/** The raw call and its answer, kept for when something needs checking. */
function TechnicalDetails({ call }: { call: Call | null | undefined }) {
  return (
    <details className="technical">
      <summary>Technical details</summary>
      {call === undefined ? (
        <p role="status">Loading the call...</p>
      ) : call === null ? (
        <p className="faint" data-testid="call">
          No call yet. It is recorded the moment this stage starts.
        </p>
      ) : (
        <div className="call-json" data-testid="call">
          <p className="faint">
            {call.connector} · <code>{call.tool}</code> · {call.status} · attempt {call.attempts}
            {call.polls ? ` · polled ${call.polls} times` : ""}
          </p>
          <div>
            <h4>Request</h4>
            <pre data-testid="call-request">{JSON.stringify(call.request, null, 2)}</pre>
          </div>
          <div>
            <h4>Response</h4>
            <pre data-testid="call-response">{call.response ? JSON.stringify(call.response, null, 2) : "Waiting for an answer."}</pre>
          </div>
        </div>
      )}
    </details>
  );
}

export type PanelProps = {
  run: Run;
  stage: Stage;
  worldId: string;
  made: ArtifactFile[];
  agents: Agent[];
  log: LogEntry[];
  tasks: Task[];
  verdicts: Verdict[];
  call: Call | null | undefined;
  onTaskAdded: (task: Task) => void;
  /** What the panel leads with: the open gate's question and answer, or the run's result. */
  lead?: ReactNode;
  /** The gate's answer is the lead, so the gate panel does not repeat its question. */
  answering?: boolean;
  /** This step made the result shown above, so its player is not repeated here. */
  resultShown?: boolean;
};

/** Part 7: what the selected step did and made, by its type, then its live log (part 8). */
export function StagePanel(p: PanelProps) {
  const { run, stage, worldId, made, call } = p;
  const kind = kindOf(stage);
  const connector = stage.type === "connector";
  let inside;
  if (stage.type === "swarm") {
    inside = (
      <>
        <AgentCards agents={p.agents} log={p.log} tasks={p.tasks} />
        <Whiteboard runId={run.id} stage={stage} tasks={p.tasks} onAdded={p.onTaskAdded} />
        <StageOutput verdicts={p.verdicts} artifacts={made} />
      </>
    );
  } else if (stage.type === "gate") {
    inside = <GatePanel run={run} stage={stage} open={!!p.answering} />;
  } else if (stage.type === "retro") {
    inside = <RetroPanel run={run} stage={stage} />;
  } else if (kind === "speech") {
    const timings = made.find((a) => a.name === "timings.json");
    inside = p.resultShown && timings ? (
      <p className="faint">Its takes are the result shown above.</p>
    ) : timings ? (
      <SpeechTakes timings={timings} worldId={worldId} />
    ) : <p className="faint">No narration yet. The takes appear here once the engine has voiced the script.</p>;
  } else if (kind === "build") {
    inside = <BuildPanel call={call} log={p.log} agents={p.agents} made={made} worldId={worldId} shown={p.resultShown} />;
  } else if (kind === "render") {
    inside = (
      <>
        <RenderProgress call={call} />
        <Video made={made} worldId={worldId} shown={p.resultShown} />
      </>
    );
  } else if (isPublish(stage, made)) {
    inside = <PublishPanel made={made} call={call} />;
  } else {
    const out = work(made);
    inside = out.length ? (
      p.resultShown ? (
        <p className="faint">It made {out.map((a) => a.name).join(", ")}, the result shown above.</p>
      ) : (
        out.map((a) => <Made key={a.path} a={a} worldId={worldId} />)
      )
    ) : (
      <p className="faint">{call?.status === "polling" ? `Working: ${Math.round((call.progress ?? 0) * 100)}%` : "Nothing made yet."}</p>
    );
  }
  const logged = stage.type === "swarm" || stage.type === "retro" || (connector && kind !== "build" && p.log.length > 0);
  return (
    <section className="panel" aria-label={`Step ${stage.index + 1}, ${stage.name}`} data-testid="stage-panel">
      <div className="panel-head">
        <h2>{stage.name}</h2>
        <span className="faint">
          {kind} · step {stage.index + 1} of {run.stages.length} · {stepMeta(stage, run.status === "queued")}
        </span>
      </div>
      {p.lead}
      {stage.child && (
        <p data-testid="child-run">
          Child run:{" "}
          <Link to="/worlds/$worldId/runs/$runId" params={{ worldId: stage.child.world_id, runId: stage.child.id }}>
            {stage.child.name}
          </Link>
        </p>
      )}
      {(stage.notes ?? []).map((note, i) => (
        <p className="faint" key={i}>
          Sent back with: {note}
        </p>
      ))}
      {stage.error && (
        <div className="stage-failure" role="alert">
          <p>{stage.error.split("\n")[0]}</p>
          {stage.error.includes("\n") && (
            <pre className="stage-log" data-testid="stage-log">
              {stage.error.split("\n").slice(1).join("\n")}
            </pre>
          )}
        </div>
      )}
      {inside}
      {logged && <LiveLog log={p.log} tasks={p.tasks} />}
      {connector && <TechnicalDetails call={call} />}
    </section>
  );
}
