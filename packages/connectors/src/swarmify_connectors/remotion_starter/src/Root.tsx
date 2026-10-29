import { Composition } from "remotion";
import { Main } from "./Main";

// The render passes {script, scriptPath} as input props; read them with getInputProps().
export const Root = () => (
  <Composition
    id="Main"
    component={Main}
    durationInFrames={90}
    fps={30}
    width={1080}
    height={1920}
  />
);
