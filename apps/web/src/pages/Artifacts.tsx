import { Link } from "@tanstack/react-router";
import { Fragment, type ReactNode } from "react";

import type { ArtifactFile } from "../api/events";
import { cap, the } from "../skin";

/** A run file, served by the API under its path in the world folder. */
export const fileUrl = (worldId: string, path: string) =>
  `/api/worlds/${worldId}/files/${path.split("/").map(encodeURIComponent).join("/")}`;

/** The raw file behind an artifact. A binary is listed by its sidecar (`short.mp4.md`), so the
 * bytes live one `.md` short of its path. */
export const rawUrl = (worldId: string, a: Pick<ArtifactFile, "path" | "name">) =>
  fileUrl(worldId, a.path.endsWith(`/${a.name}.md`) ? a.path.slice(0, -3) : a.path);

/** A document the reader shows: markdown or plain text. */
export const DOCUMENT = /\.(md|txt)$/i;

/** Open a file: a document in the in-app reader, anything else as the raw file in a new tab. */
export function FileLink({ a, worldId, className, children }: { a: Pick<ArtifactFile, "path" | "name">; worldId: string; className?: string; children: ReactNode }) {
  if (DOCUMENT.test(a.name)) {
    return (
      <Link to="/worlds/$worldId/read" params={{ worldId }} search={{ path: a.path }} className={className}>
        {children}
      </Link>
    );
  }
  return (
    <a href={rawUrl(worldId, a)} target="_blank" rel="noreferrer" className={className}>
      {children}
    </a>
  );
}

export const VIDEO = /\.(mp4|webm|mov|m4v)$/i;
export const IMAGE = /\.(png|jpe?g|gif|webp|svg|avif)$/i;
export const AUDIO = /\.(wav|mp3|m4a|ogg|flac|aac)$/i;

/** `runs/<run>/<stage>/notes/trends.md#p3` as "trends.md, paragraph 3", linking to the note. */
function Source({ ref, worldId }: { ref: string; worldId: string }) {
  const [path = "", p] = ref.split("#p");
  return (
    // A new tab keeps a half-made pick on the gate.
    <a href={`/worlds/${worldId}/read?${new URLSearchParams({ path })}`} target="_blank" rel="noreferrer">
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

/** Choosable cards for a pick gate as a ranked radio list (story 32): rank, title and hook or
 * summary, with the judge's reason and the note paragraphs it came from folded away (story 33). */
export function ArtifactCards({ cards, worldId }: { cards: ArtifactFile[]; worldId: string }) {
  return (
    <fieldset className="pick-cards" data-testid="pick-cards">
      <legend>Your pick</legend>
      {cards.map((a) => {
        const sources = (a.fields.sources as string[] | undefined) ?? [];
        const hook = typeof a.fields.hook === "string" ? a.fields.hook : summary(a.body);
        const why = [a.fields.reason, a.fields.why].find((w) => typeof w === "string" && w) as string | undefined;
        return (
          <label key={a.path} className="pick-card" data-testid="pick-card">
            <input type="radio" name="artifact" value={a.path} required />
            <span className="rank">
              {a.fields.rank != null ? `${cap(the("judge"))}'s #${String(a.fields.rank)} of ${cards.length}` : a.type}
            </span>
            <b>{a.title}</b>
            {hook && <span className="hook">{hook}</span>}
            <details>
              <summary>Why it ranked here</summary>
              {why && <p>Why: {why}</p>}
              <p>
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
              </p>
            </details>
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
              <>
                <TypedText body={a.body} />
                <Markdown text={a.body} base={{ worldId, path: a.path }} />
              </>
            ) : (
              <FileLink a={a} worldId={worldId}>
                Open {a.name}
              </FileLink>
            )}
          </figure>
        );
      })}
    </section>
  );
}

/** A typed artifact's top-level text from its fenced JSON block, as the voice a script's judge
 * chose, so it shows on the gate before the long value below. */
export function TypedText({ body }: { body: string }) {
  const block = /```json[ \t]*\n([\s\S]*?)\n```/.exec(body)?.[1];
  let value: unknown = null;
  try {
    value = block ? JSON.parse(block) : null;
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const text = Object.entries(value).filter((e): e is [string, string] => typeof e[1] === "string");
  if (!text.length) return null;
  return (
    <dl className="typed-text" data-testid="typed-text">
      {text.map(([k, v]) => (
        <Fragment key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

/** Markdown as agents write it: headings, paragraphs, lists, fenced code, bold, code spans and
 * web links and images, and tables. Built as elements, never as HTML, so an agent's text cannot
 * script the page. Given the document's place in its world (`base`), an image linked by a path
 * relative to the document loads through the files route. ponytail: no nesting; a markdown
 * library when documents need it. */
export function Markdown({ text, base }: { text: string; base?: DocBase }) {
  const blocks = text
    .split(/\n\s*\n/)
    .map((b) => b.trim())
    .filter(Boolean);
  return <div className="md">{blocks.map((b, i) => block(b, i, base))}</div>;
}

/** Where a document lives: its world and its path inside the world folder. */
export type DocBase = { worldId: string; path: string };

/** The URL an image source loads from, or null to show its alt text: https as written, a path
 * relative to the document resolved against its folder. Refused: other schemes, absolute paths,
 * backslashes, and anything that climbs out of the world folder. */
export function imageSrc(src: string, base?: DocBase): string | null {
  if (/^https:\/\//i.test(src)) return src;
  if (!base || /^[a-z][a-z0-9+.-]*:/i.test(src) || src.startsWith("/") || src.includes("\\")) return null;
  const parts = base.path.split("/").slice(0, -1);
  for (const seg of src.split("/")) {
    if (seg === "..") {
      if (!parts.length) return null;
      parts.pop();
    } else if (seg && seg !== ".") parts.push(seg);
  }
  return parts.length ? fileUrl(base.worldId, parts.join("/")) : null;
}

function block(b: string, key: number, base?: DocBase): ReactNode {
  const heading = /^(#{1,6})\s+(.*)$/s.exec(b);
  if (heading) {
    // The page owns h1 and h2; a document's own levels start at h3.
    const Tag = `h${Math.min((heading[1] ?? "#").length + 2, 6)}` as "h3" | "h4" | "h5" | "h6";
    return <Tag key={key}>{inline(heading[2] ?? "", base)}</Tag>;
  }
  if (b.startsWith("```")) return <pre key={key}>{b.replace(/^```[^\n]*\n?/, "").replace(/\n?```$/, "")}</pre>;
  const lines = b.split("\n");
  if (lines.length > 1 && lines.every((l) => l.trim().startsWith("|")) && /^[\s|:-]+$/.test(lines[1] ?? "")) {
    const cells = (l: string) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    return (
      // A wide table scrolls inside itself, never the page.
      <div key={key} className="scroll-x">
        <table>
          <thead>
            <tr>{cells(lines[0] ?? "").map((c, i) => <th key={i}>{inline(c, base)}</th>)}</tr>
          </thead>
          <tbody>
            {lines.slice(2).map((l, r) => (
              <tr key={r}>{cells(l).map((c, i) => <td key={i}>{inline(c, base)}</td>)}</tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  if (lines.every((l) => /^[-*] /.test(l))) {
    return <ul key={key}>{lines.map((l, i) => <li key={i}>{inline(l.slice(2), base)}</li>)}</ul>;
  }
  if (lines.every((l) => /^\d+\. /.test(l))) {
    return <ol key={key}>{lines.map((l, i) => <li key={i}>{inline(l.replace(/^\d+\. /, ""), base)}</li>)}</ol>;
  }
  return <p key={key}>{inline(b, base)}</p>;
}

function inline(s: string, base?: DocBase): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*|`[^`]+`|!\[[^\]]*\]\([^)\s]+\)|\[[^\]]*\]\(https?:\/\/[^)\s]+\))/g).map((part, i) => {
    if (part.startsWith("**")) return <b key={i}>{part.slice(2, -2)}</b>;
    if (part.startsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    const image = /^!\[([^\]]*)\]\(([^)\s]+)\)$/.exec(part);
    if (image) {
      const src = imageSrc(image[2] ?? "", base);
      return src ? <img key={i} src={src} alt={image[1]} /> : image[1];
    }
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
