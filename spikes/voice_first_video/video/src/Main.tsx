// PROTOTYPE: voice clips placed on the timeline, karaoke captions, music ducked under speech.
import { AbsoluteFill, Audio, Sequence, interpolate, spring, staticFile, useCurrentFrame, useVideoConfig } from "remotion";
import { createTikTokStyleCaptions } from "@remotion/captions";
import timeline from "../public/timeline.json";

// Pages never cross a line: each spoken line is split into pages of its own words.
const lineIds = [...new Set(timeline.captions.map((c) => c.line))];
const pages = lineIds.flatMap((id) => createTikTokStyleCaptions({
  captions: timeline.captions.filter((c) => c.line === id), combineTokensWithinMilliseconds: 1200 }).pages);
const sceneAt = (t: number) => timeline.scenes.find((s) => t >= s.start && t < s.end)?.id ?? "fact";
const reveal = timeline.captions.find((c) => c.text.trim().startsWith("There"))!.startMs / 1000;

const Leaf = ({ x, y, r, c, rot }: { x: number; y: number; r: number; c: string; rot: number }) => (
  <div style={{ position: "absolute", left: x, top: y, width: r * 2, height: r * 1.3, background: c,
    borderRadius: "50% 0 50% 0", transform: `rotate(${rot}deg)` }} />
);
const LEAVES = Array.from({ length: 26 }, (_, i) => ({
  x: 120 + ((i * 137) % 760), y: 1080 + ((i * 89) % 260), r: 60 + ((i * 31) % 40),
  c: ["#E8613C", "#F2A93B", "#C8402B", "#F7C548"][i % 4], rot: (i * 47) % 360,
}));

const Hedgehog = ({ s }: { s: number }) => (
  <div style={{ position: "absolute", left: 360, top: 980, transform: `scale(${s})`, transformOrigin: "50% 100%" }}>
    <div style={{ width: 360, height: 220, background: "#7A4E2D", borderRadius: "180px 180px 120px 120px",
      boxShadow: "0 -18px 0 -4px #5C3A21" }} />
    <div style={{ position: "absolute", left: 250, top: 110, width: 150, height: 110, background: "#E9C9A1", borderRadius: "50%" }} />
    <div style={{ position: "absolute", left: 330, top: 140, width: 18, height: 6, background: "#3B2A1E", borderRadius: 4 }} />
    <div style={{ position: "absolute", left: 392, top: 158, width: 22, height: 18, background: "#3B2A1E", borderRadius: "50%" }} />
  </div>
);

export const Main = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;
  const ms = t * 1000;
  const speaking = timeline.clips.some((c) => t >= c.from && t < c.from + (c.trimAfter - c.trimBefore));
  const scene = sceneAt(t);
  const pop = spring({ frame: frame - Math.round(reveal * fps), fps, config: { damping: 9 } });
  const page = pages.find((p) => ms >= p.startMs && ms < p.startMs + p.durationMs);
  const sky = scene === "fact" ? "#2C3E73" : "#8FD3F4";
  return (
    <AbsoluteFill style={{ background: `linear-gradient(${sky}, #FFF4D6 70%)`, fontFamily: "Arial Rounded MT Bold, Arial" }}>
      <Audio src={staticFile("music.wav")} volume={() => (speaking ? 0.12 : 0.45)} />
      {timeline.clips.map((c, i) => (
        <Sequence key={i} from={Math.round(c.from * fps)} durationInFrames={Math.ceil((c.trimAfter - c.trimBefore) * fps) + 1}>
          <Audio src={staticFile(c.src)} trimBefore={Math.round(c.trimBefore * fps)} trimAfter={Math.round(c.trimAfter * fps)} />
        </Sequence>
      ))}
      <div style={{ position: "absolute", left: 640, top: 1180, width: 380, height: 110, background: "#6B4226", borderRadius: 55 }} />
      {LEAVES.map((l, i) => {
        const blow = t >= reveal ? interpolate(t - reveal, [0, 1.2], [0, 1], { extrapolateRight: "clamp" }) : 0;
        const wiggle = scene === "search" ? Math.sin(frame / 4 + i) * 6 : 0;
        return <Leaf key={i} {...l} x={l.x + (l.x - 540) * blow * 1.6} y={l.y - blow * 300 + wiggle} rot={l.rot + blow * 90} />;
      })}
      {t >= reveal && <Hedgehog s={pop} />}
      <div style={{ position: "absolute", top: 260, width: "100%", textAlign: "center", fontSize: 72, color: "#1F2A44",
        fontWeight: 900, padding: "0 70px", boxSizing: "border-box", opacity: interpolate(frame, [0, 12], [0, 1]) }}>
        {timeline.title}
      </div>
      {page && (
        <div style={{ position: "absolute", top: 620, width: "100%", textAlign: "center", fontSize: 104, fontWeight: 900,
          padding: "0 80px", boxSizing: "border-box", lineHeight: 1.15 }}>
          {page.tokens.map((tok, i) => {
            const on = ms >= tok.fromMs && ms < tok.toMs;
            return <span key={i} style={{ color: on ? "#FF6B35" : "#FFFFFF", WebkitTextStroke: "6px #1F2A44",
              paintOrder: "stroke", display: "inline-block", whiteSpace: "pre", transform: `scale(${on ? 1.12 : 1})` }}>{tok.text}</span>;
          })}
        </div>
      )}
    </AbsoluteFill>
  );
};
