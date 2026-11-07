The video is voice first: $file is the narration, the speech stage's timings.json, one take per
scene, with the takes in $dir/narration/ beside it (each take's `file` is relative to $dir, as
narration/hook.wav).
- Read them in place by these absolute paths; do not copy them into the project. The render
  reads the run's folders, and a finish step also finds them through BLENDER_RUN. When the
  Template's guide names a tool or setting that takes the narration (a clock that times shots
  to the voice), give it $file.
- The voice sets the length: each line is its take from `cut_in` to `cut_out` (seconds), placed
  after the previous line's clip and its pause (none after a line with `joined_next: true`).
  Shots are cut to that timeline, never the voice to the shots.
- A line's `words` (ms from its take's start) time what happens on screen to the word; its
  `directions` say how it was voiced, for the picture to match.
- The sound is the finish step's: when render.json has none, write one (a `python3` script, with
  ffmpeg and ffprobe on its PATH) that joins the shots in BLENDER_SHOTS (`<id>.mp4` each), lays
  each line's clip of its take at its place on the timeline and writes BLENDER_OUTPUT.
