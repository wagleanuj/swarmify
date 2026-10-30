import type { ArtifactFile, Verdict } from "../api/events";
import { cap, the } from "../skin";

/** Loops a stage may take: the first verdict plus three gap loops. Mirrors swarmify_swarm.MAX_LOOPS. */
const VERDICTS = 4;

/** `runs/<run>/<stage>/notes/trends.md#p3` as "trends.md, paragraph 3". */
function source(ref: string): string {
  const [path = "", p] = ref.split("#p");
  return `${path.split("/").pop()}, paragraph ${p}`;
}

/** The judge's verdicts on a swarm stage and the typed artifacts it projected (ADR 0011). */
export function StageOutput({ verdicts, artifacts }: { verdicts: Verdict[]; artifacts: ArtifactFile[] }) {
  const last = verdicts.at(-1);
  const typed = artifacts
    .filter((a) => a.type !== "verdict" && a.type !== "file")
    .sort((a, b) => Number(a.fields.rank ?? 1e9) - Number(b.fields.rank ?? 1e9));
  const files = artifacts.filter((a) => a.type === "file");
  return (
    <>
      <section className="agents" aria-label="Verdict" data-testid="verdict">
        <h3>{cap(the("judge"))}'s verdict</h3>
        {!last ? (
          <p className="faint">No verdict yet. {cap(the("judge"))} judges once every task on the whiteboard is done.</p>
        ) : (
          <>
            <p>
              <span className={`status status-${last.passed ? "done" : "failed"}`}>{last.passed ? "pass" : "fail"}</span>{" "}
              on loop {last.loop} of {VERDICTS}.
              {!last.passed &&
                (last.loop < VERDICTS
                  ? " The gap tasks below went back on the whiteboard."
                  : " That was the last loop, so the stage failed.")}
            </p>
            <ul>
              {last.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
            {last.gaps.length > 0 && (
              <p data-testid="gaps">Gap tasks: {last.gaps.join("; ")}</p>
            )}
            {verdicts.length > 1 && (
              <ol className="faint" aria-label="Earlier verdicts">
                {verdicts.slice(0, -1).map((v) => (
                  <li key={v.id}>
                    Loop {v.loop}: {v.passed ? "pass" : "fail"}. {v.reasons.join(" ")}
                  </li>
                ))}
              </ol>
            )}
          </>
        )}
      </section>
      <section className="agents" aria-label="Artifacts" data-testid="artifacts">
        <h3>Artifacts</h3>
        {typed.length === 0 && files.length === 0 && (
          <p className="faint">Nothing yet. On a pass, {the("judge")} turns the stage's notes into its declared output.</p>
        )}
        {typed.length > 0 && (
          <ul className="cards">
            {typed.map((a) => {
              const sources = (a.fields.sources as string[] | undefined) ?? [];
              return (
                <li key={a.path} className="card" data-testid="artifact-card">
                  <span className="faint">
                    {a.type}
                    {a.fields.rank != null ? ` · #${String(a.fields.rank)} of ${typed.length}` : ""}
                  </span>
                  <b>{a.title}</b>
                  {typeof a.fields.hook === "string" && <span>Hook: {a.fields.hook}</span>}
                  {typeof a.fields.why === "string" && <span>Why: {a.fields.why}</span>}
                  {typeof a.fields.views === "number" && (
                    <span data-testid="views">
                      {a.fields.views.toLocaleString("en")} views as of{" "}
                      {String(a.fields.views_at ?? "").slice(0, 10)}
                    </span>
                  )}
                  {a.type !== "publish" && (
                    <span className="faint">
                      {sources.length ? `From ${sources.map(source).join("; ")}` : "No source paragraph cited."}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
        {files.length > 0 && (
          <p className="faint">
            Files:{" "}
            {files
              .map((f) => (f.fields.deleted ? `${f.name} (deleted by retention, record kept)` : f.name))
              .join(", ")}
          </p>
        )}
      </section>
    </>
  );
}
