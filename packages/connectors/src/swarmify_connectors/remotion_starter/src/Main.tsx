import { AbsoluteFill, getInputProps, interpolate, useCurrentFrame } from "remotion";

export const Main = () => {
  const frame = useCurrentFrame();
  const { script } = getInputProps() as { script?: string };
  const title = (script ?? "").split("\n").find((line) => line.trim()) ?? "Swarmify";
  return (
    <AbsoluteFill style={{ background: "#111", justifyContent: "center", padding: 80 }}>
      <h1 style={{ color: "white", fontSize: 96, opacity: interpolate(frame, [0, 20], [0, 1]) }}>
        {title.replace(/^#+\s*/, "")}
      </h1>
    </AbsoluteFill>
  );
};
