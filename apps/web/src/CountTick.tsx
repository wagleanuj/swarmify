import { useState } from "react";

/** A count that ticks: when it changes, the old number slides out and the new one slides in,
 * up when it rose and down when it fell. The first value just shows. `format` writes it out
 * (money, say); the direction still comes from the number. */
export function CountTick({ value, format = String }: { value: number; format?: (n: number) => string }) {
  const [shown, setShown] = useState<{ now: number; was?: number }>({ now: value });
  if (shown.now !== value) setShown({ now: value, was: shown.now });
  const was = shown.was;
  return (
    <span className="tick" data-dir={was !== undefined && value < was ? "down" : "up"}>
      {was !== undefined && (
        <span key={`${was}-${value}`} className="tick-out" aria-hidden="true" onAnimationEnd={() => setShown((s) => ({ now: s.now }))}>
          {format(was)}
        </span>
      )}
      <span key={value} className={was !== undefined ? "tick-in" : undefined}>
        {format(value)}
      </span>
    </span>
  );
}
