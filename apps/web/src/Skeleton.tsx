import type { CSSProperties, ReactNode } from "react";

const size = (v: number | string | undefined) => (typeof v === "number" ? `${v}px` : v);

/** A placeholder block where data will be, at the size the data will take: a `--raised` block
 * that breathes slowly. `lines` draws that many text lines instead, the last one shorter.
 * Hidden from screen readers; put skeletons inside `<Loading>`, which announces the wait. */
export function Skeleton({ w, h, lines, className = "" }: { w?: number | string; h?: number | string; lines?: number; className?: string }) {
  const style = { "--sk-w": size(w), "--sk-h": size(h) } as CSSProperties;
  if (!lines) return <span className={`sk ${className}`.trim()} style={style} aria-hidden="true" />;
  return (
    <span className={`sk-lines ${className}`.trim()} style={style} aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <span key={i} className="sk" />
      ))}
    </span>
  );
}

/** The region a page's skeletons sit in while its data loads: `aria-busy`, with one visually
 * hidden "Loading" status for screen readers. Static parts of the page stay outside it. */
export function Loading({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={className} aria-busy="true">
      <span className="visually-hidden" role="status">
        Loading
      </span>
      {children}
    </div>
  );
}
