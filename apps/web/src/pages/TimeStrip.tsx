import { clock, type Run } from "../api/runs";
import { stageSeconds } from "./StagePanel";

type Stage = Run["stages"][number];

/** How long a step took, or has taken so far, as its segment names it. */
function spent(s: Stage): string {
  const secs = stageSeconds(s);
  if (secs === null) return s.status === "skipped" ? "skipped" : "not started";
  return s.status === "waiting" ? `waiting ${clock(secs)}` : clock(secs);
}

/** The run's steps side by side, each as wide as its time. Time waiting on a person is capped at
 * 1.5 times the longest working step so it cannot hide the work; its label keeps the real figure.
 * Steps not started get a fixed dashed slot. Without `labels` it is the small strip of a list row. */
export function TimeStrip({
  stages,
  selected,
  onSelect,
  labels = true,
}: {
  stages: Stage[];
  selected?: string;
  onSelect?: (id: string) => void;
  labels?: boolean;
}) {
  const secs = (s: Stage) => stageSeconds(s) ?? 0;
  const cap = Math.max(60, ...stages.filter((s) => s.type !== "gate").map(secs)) * 1.5;
  const shown = (s: Stage) => (s.type === "gate" ? Math.min(secs(s), cap) : secs(s));
  const total = stages.reduce((sum, s) => sum + shown(s), 0) || 1;
  return (
    <div className={`strip${labels ? "" : " strip-bare"}`} role="group" aria-label="Time per step" data-testid="strip">
      {stages.map((s) => {
        const started = stageSeconds(s) !== null;
        const share = Math.max(shown(s) / total, 0.03);
        const label = s.type === "gate" && started ? `${s.name} · ${spent(s)}` : s.name;
        const className = `strip-seg seg-${s.status}${s.type === "gate" ? " seg-gate" : ""}`;
        const style = started ? { flex: `${share} 1 0` } : undefined;
        const title = `${s.name} · ${spent(s)}`;
        const text = labels && <span>{label}</span>;
        return onSelect ? (
          <button key={s.id} type="button" className={className} style={style} title={title} aria-label={title} aria-pressed={s.id === selected} onClick={() => onSelect(s.id)}>
            {text}
          </button>
        ) : (
          <span key={s.id} className={className} style={style} title={title} aria-label={title} role="img">
            {text}
          </span>
        );
      })}
    </div>
  );
}
