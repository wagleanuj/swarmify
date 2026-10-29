// PROTOTYPE: the composition's length comes from the voice timeline (voice first).
import { Composition } from "remotion";
import { Main } from "./Main";
import timeline from "../public/timeline.json";

const FPS = 30;
export const Root = () => (
  <Composition id="Main" component={Main} fps={FPS} width={1080} height={1920}
    durationInFrames={Math.ceil(timeline.duration * FPS)} />
);
