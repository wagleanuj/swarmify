import type { ReactNode } from "react";

import type { ArtifactFile } from "../api/events";

/** A run file, served by the API under its path in the world folder. */
export const fileUrl = (worldId: string, path: string) =>
  `/api/worlds/${worldId}/files/${path.split("/").map(encodeURIComponent).join("/")}`;

/** The raw file behind an artifact. A binary is listed by its sidecar (`short.mp4.md`), so the
 * bytes live one `.md` short of its path. */
export const rawUrl = (worldId: string, a: ArtifactFile) =>
  fileUrl(worldId, a.path.endsWith(`/${a.name}.md`) ? a.path.slice(0, -3) : a.path);

/** What the run wrote about itself, not something to choose or review. */
export const RECORD = new Set(["verdict", "decision", "note"]);
export const VIDEO = /\.(mp4|webm|mov|m4v)$/i;
export const IMAGE = /\.(png|jpe?g|gif|webp|svg|avif)$/i;
export const AUDIO = /\.(wav|mp3|m4a|ogg|flac|aac)$/i;

const byRank = (a: ArtifactFile, b: ArtifactFile) =>
  Number(a.fields.rank ?? 1e9) - Number(b.fields.rank ?? 1e9);

/** The cards a pick gate chooses from: the typed artifacts the judge projected from the earlier
 * stages (an idea per card), his rank first. A plain file is not a choice, so a stage with no
 * declared output leaves the gate its free-text pick. */
export function choosable(artifacts: ArtifactFile[]): ArtifactFile[] {
  return artifacts
    .filter((a) => !RECORD.has(a.type) && a.type !== "file" && !a.fields.deleted)
    .sort(byRank);
}

/** What an approve gate reviews: the artifacts of the latest earlier stage that made any. */
export function underReview(artifacts: ArtifactFile[]): ArtifactFile[] {
  const work = artifacts.filter((a) => !RECORD.has(a.type));
  const last = work.at(-1)?.stage;
  return work.filter((a) => a.stage === last);
}

/** `runs/<run>/<stage>/notes/trends.md#p3` as "trends.md, paragraph 3", linking to the note. */
function Source({ ref, worldId }: { ref: string; worldId: string }) {
  const [path = "", p] = ref.split("#p");
  return (
    <a href={fileUrl(worldId, path)} target="_blank" rel="noreferrer">
      {path.split("/").pop()}, paragraph {p}
    </a>
  );
}

/** The first paragraph of the body that is not its heading, for a card with no hook. */
export function summary(body: string): string {
  const para = body
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .find((b) => b && !b.startsWith("#"));
  return (para ?? "").replace(/\s+/g, " ").slice(0, 200);
}

/** Choosable cards for a pick gate, a radio each (story 53): title, hook or summary, the judge's
 * rank and reason where given, and the note paragraph it came from. */
export function ArtifactCards({ cards, worldId }: { cards: ArtifactFile[]; worldId: string }) {
  return (
    <fieldset className="pick-cards" data-testid="pick-cards">
      <legend>Your pick</legend>
      {cards.map((a) => {
        const sources = (a.fields.sources as string[] | undefined) ?? [];
        const hook = typeof a.fields.hook === "string" ? a.fields.hook : summary(a.body);
        const why = [a.fields.reason, a.fields.why].find((w) => typeof w === "string" && w) as string | undefined;
        return (
          <label key={a.path} className="card pick-card" data-testid="pick-card">
            <input type="radio" name="artifact" value={a.path} required />
            <span className="faint">
              {a.fields.rank != null ? `Erlich's #${String(a.fields.rank)} of ${cards.length}` : a.type}
            </span>
            <b>{a.title}</b>
            {hook && <span>{hook}</span>}
            {why && <span className="faint">Why: {why}</span>}
            <span className="faint">
              {sources.length ? (
                <>
                  From{" "}
                  {sources.map((s, i) => (
                    <span key={s}>
                      {i > 0 && "; "}
                      <Source ref={s} worldId={worldId} />
                    </span>
                  ))}
                </>
              ) : (
                "No source paragraph cited."
              )}
            </span>
          </label>
        );
      })}
    </fieldset>
  );
}

/** The artifacts under review, in place (story 54): a player for a video, the image itself,
 * rendered markdown for a document, a link for anything else. */
export function ArtifactView({ artifacts, worldId }: { artifacts: ArtifactFile[]; worldId: string }) {
  return (
    <section className="artifact-view" aria-label="Under review" data-testid="under-review">
      {artifacts.map((a) => {
        const url = rawUrl(worldId, a);
        return (
          <figure key={a.path}>
            <figcaption>
              <b>{a.title}</b> <span className="faint">{a.type}</span>
            </figcaption>
            {a.fields.deleted ? (
              <p className="faint">{a.name} was deleted by retention; its record stays.</p>
            ) : a.type === "video" || VIDEO.test(a.name) ? (
              <video controls preload="metadata" src={url} data-testid="player" />
            ) : IMAGE.test(a.name) ? (
              <img src={url} alt={a.title} />
            ) : a.name.endsWith(".md") ? (
              <Markdown text={a.body} />
            ) : (
              <a href={url} target="_blank" rel="noreferrer">
                Open {a.name}
              </a>
            )}
          </figure>
        );
      })}
    </section>
  );
}

/** Markdown as agents write it: headings, paragraphs, lists, fenced code, bold, code spans and
 * web links. Built as elements, never as HTML, so an agent's text cannot script the page.
 * ponytail: no tables, nesting or images; a markdown library when documents need them. */
export function Markdown({ text }: { text: string }) {
  const blocks = text
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean);
  return <div className="md">{blocks.map((b, i) => block(b, i))}</div>;
}

function block(b: string, key: number): ReactNode {
  const heading = /^(#{1,6})\s+(.*)$/s.exec(b);
  if (heading) {
    // The page owns h1 and h2; a document's own levels start at h3.
    const Tag = `h${Math.min((heading[1] ?? "#").length + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
    return <Tag key={key}>{inline(heading[2] ?? "")}</Tag>;
  }
  if (b.startsWith("```")) return <pre key={key}>{b.replace(/^```[^\n]*\n?/, "").replace(/\n?```$/, "")}</pre>;
  const lines = b.split("\n");
  if (lines.every((l) => /^[-*] /.test(l))) {
    return <ul key={key}>{lines.map((l, i) => <li key={i}>{inline(l.slice(2))}</li>)}</ul>;
  }
  if (lines.every((l) => /^\d+\. /.test(l))) {
    return <ol key={key}>{lines.map((l, i) => <li key={i}>{inline(l.replace(/^\d+\. /, ""))}</li>)}</ol>;
  }
  return <p key={key}>{inline(b)}</p>;
}

function inline(s: string): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(https?:\/\/[^)\s]+\))/g).map((part, i) => {
    if (part.startsWith("**")) return <b key={i}>{part.slice(2, -2)}</b>;
    if (part.startsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    const link = /^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/.exec(part);
    if (link) {
      return (
        <a key={i} href={link[2]} target="_blank" rel="noreferrer">
          {link[1]}
        </a>
      );
    }
    return part;
  });
}
