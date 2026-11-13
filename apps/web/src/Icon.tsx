/** The one icon set: outline glyphs on a 24 grid, 1.5px stroke in `currentColor`, drawn at 16 or
 * 20px. Every arrow, chevron, kebab and nav glyph in the app is one of these. */
const PATHS = {
  inbox: "M4 13l2.5-8h11l2.5 8v6H4zM4 13h5l1 2h4l1-2h5",
  home: "M4 11l8-6.5 8 6.5v9h-5.5v-6h-5v6H4z",
  runs: "M7 5l12 7-12 7z",
  play: "M7 5l12 7-12 7z",
  pause: "M7 5h3v14H7zM14 5h3v14h-3z",
  stop: "M6 6h12v12H6z",
  workflows: "M3.5 6a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0M15.5 18a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0M8.5 6H14a4 4 0 0 1 4 4v5.5",
  library: "M5 4v16M10 4v16M15 5l4 14",
  assets: "M6 3h8l4 4v14H6zM14 3v4h4",
  more: "M3.5 12a1.25 1.25 0 1 0 2.5 0a1.25 1.25 0 1 0-2.5 0M10.75 12a1.25 1.25 0 1 0 2.5 0a1.25 1.25 0 1 0-2.5 0M18 12a1.25 1.25 0 1 0 2.5 0a1.25 1.25 0 1 0-2.5 0",
  back: "M15 5l-7 7 7 7",
  next: "M9 5l7 7-7 7",
  chevron: "M7 10l5 5 5-5",
  check: "M5 12.5l4.5 4.5L19 7.5",
  x: "M6 6l12 12M18 6L6 18",
  retry: "M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4v4.5H15",
  send: "M4 12l16-8-6 16-2.5-6.5z",
  edit: "M4 20h4L19 9l-4-4L4 16zM13 7l4 4",
  copy: "M8 8h11v11H8zM5 15V5h10",
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  upload: "M12 15V4M7 9l5-5 5 5M5 20h14",
  search: "M5 11a6 6 0 1 0 12 0a6 6 0 1 0-12 0M15.5 15.5L20 20",
  filter: "M4 6h16M7 12h10M10 18h4",
  bell: "M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15zM10 20.5h4",
  settings: "M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0M12 2.5v3M12 18.5v3M4.6 4.6l2.1 2.1M17.3 17.3l2.1 2.1M2.5 12h3M18.5 12h3M4.6 19.4l2.1-2.1M17.3 6.7l2.1-2.1",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4",
  people: "M6 8a3 3 0 1 0 6 0a3 3 0 1 0-6 0M3 20c0-3 3-5 6-5s6 2 6 5M16 5a3 3 0 0 1 0 6M18 15c2 1 3 3 3 5",
  memory: "M8 7l8 1M7 8l2 8M17 10l-6 7M4 6a2 2 0 1 0 4 0a2 2 0 1 0-4 0M16 8a2 2 0 1 0 4 0a2 2 0 1 0-4 0M8 18a2 2 0 1 0 4 0a2 2 0 1 0-4 0",
  world: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18",
  office: "M4 20V8l8-4 8 4v12zM9 20v-5h6v5M8 10h2M14 10h2",
  evals: "M4 20V10M10 20V4M16 20v-7M21 20H3",
  warning: "M12 4l9 16H3zM12 10v4M12 17h.01",
  plus: "M12 5v14M5 12h14",
  info: "M3 12a9 9 0 1 0 18 0a9 9 0 1 0-18 0M12 11v5M12 8h.01",
  eye: "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0",
  "eye-off": "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0M4 4l16 16",
} as const;

export type IconName = keyof typeof PATHS;
export const ICONS = Object.keys(PATHS) as IconName[];

/** One glyph. `className` takes the state classes from motion.css: `i-pop` (a check that just
 * landed), `i-nudge` (the inbox when its count rose), `i-spin` (retry, once per mount),
 * `i-turn` (a chevron, turned while its disclosure is open). `live` adds the three-dot tick a
 * running play glyph carries. Decorative unless it has a `label`. */
export function Icon({ name, size = 16, className, live, label }: { name: IconName; size?: 16 | 20 | 24; className?: string; live?: boolean; label?: string }) {
  return (
    <svg
      className={`icon i-${name}${live ? " i-live" : ""}${className ? ` ${className}` : ""}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <path d={PATHS[name]} />
      {live && (
        <g className="i-dots" stroke="none" fill="currentColor">
          <circle cx="15" cy="20" r="1.3" />
          <circle cx="18.5" cy="20" r="1.3" />
          <circle cx="22" cy="20" r="1.3" />
        </g>
      )}
    </svg>
  );
}
