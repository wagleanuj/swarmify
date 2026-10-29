// Voice first, after the spike's composition: the narration's timeline sets the video's length,
// each line is a trimmed clip of its take, captions are karaoke paged per line, and the music bed
// ducks under speech. Expects public/timings.json and its takes (public/narration/*.wav) copied
// in, and a bed from the world's assets as public/bed.mp3. Root sets durationInFrames from
// SECONDS: durationInFrames={Math.ceil(SECONDS * fps)}.
import { AbsoluteFill, Audio, Sequence, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { createTikTokStyleCaptions, type Caption } from "@remotion/captions";
import timings from "../public/timings.json";

const LEAD = 0.4; // seconds of picture before the first word
const PAUSE = 1; // scale the script's pauses for the style: under 1 snappier, over 1 calmer

type Clip = { src: string; from: number; cutIn: number; cutOut: number };
const clips: Clip[] = [];
const pages: ReturnType<typeof createTikTokStyleCaptions>["pages"] = [];
let at = LEAD;
for (const take of timings.takes) {
  for (const line of take.lines) {
    clips.push({ src: take.file, from: at, cutIn: line.cut_in, cutOut: line.cut_out });
    // Word times are ms from the take's start: move them onto the timeline.
    const shift = (at - line.cut_in) * 1000;
    const words: Caption[] = line.words.map((w) => ({ ...w, startMs: w.startMs + shift, endMs: w.endMs + shift }));
    // One line at a time, so a page never crosses lines.
    pages.push(...createTikTokStyleCaptions({ captions: words, combineTokensWithinMilliseconds: 1200 }).pages);
    // A line joined to the next has no silence to cut in: keep the two back to back.
    at += line.cut_out - line.cut_in + (line.joined_next ? 0 : line.pause_after * PAUSE);
  }
}
export const SECONDS = at + 0.6;

export const Main = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const ms = (frame / fps) * 1000;
  const speaking = (f: number) => clips.some((c) => f / fps >= c.from && f / fps < c.from + c.cutOut - c.cutIn);
  const page = pages.find((p) => ms >= p.startMs && ms < p.startMs + p.durationMs);
  return (
    <AbsoluteFill style={{ background: "#111", justifyContent: "center" }}>
      <Audio src={staticFile("bed.mp3")} loop volume={(f) => (speaking(f) ? 0.12 : 0.45)} />
      {clips.map((c, i) => (
        <Sequence key={i} from={Math.round(c.from * fps)} durationInFrames={Math.ceil((c.cutOut - c.cutIn) * fps) + 1}>
          <Audio src={staticFile(c.src)} trimBefore={Math.round(c.cutIn * fps)} trimAfter={Math.round(c.cutOut * fps)} />
        </Sequence>
      ))}
      {page && (
        <div style={{ textAlign: "center", fontSize: 104, fontWeight: 900, padding: "0 80px", lineHeight: 1.15 }}>
          {page.tokens.map((t, i) => {
            const on = ms >= t.fromMs && ms < t.toMs;
            // inline-block drops a word's leading space unless white-space is pre
            return (
              <span key={i} style={{ display: "inline-block", whiteSpace: "pre", color: on ? "#FF6B35" : "#FFF" }}>
                {t.text}
              </span>
            );
          })}
        </div>
      )}
    </AbsoluteFill>
  );
};
