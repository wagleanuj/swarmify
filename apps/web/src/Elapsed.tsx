import { useEffect, useState } from "react";

import { clock, duration, type Run } from "./api/runs";

/** How long the run has gone, ticking each second while it is live (running or waiting for
 * you); it holds still once the run pauses or ends. Only this span re-renders on each tick. */
export function Elapsed({ run, className }: { run: Run; className?: string }) {
  const live = run.status === "running" || run.status === "waiting_gate";
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [live]);
  const seconds = duration(run, now);
  if (seconds === null) return null;
  return (
    <time className={`elapsed${className ? ` ${className}` : ""}`} data-testid="elapsed" data-live={live || undefined}>
      {clock(seconds)}
    </time>
  );
}
