The video is voice first: $file is the narration, one take per scene, and the
picture follows the voice, never the other way round.
- Copy timings.json into the project, and each take it names (`file`, relative to its folder, as
  narration/hook.wav) to the same path in the project.
- HyperFrames reads the clips, the audio and the film's length from the HTML before any script
  runs, so a script in the page cannot make them: write them into the composition from the
  timings, with a Node script such as the sample below, and run `node voice.mjs` again after
  every change to it.
- The voice sets the length: each line is a clip of its take from `cut_in` to `cut_out` (seconds),
  placed after the previous line's clip and its pause, and the root's `data-duration` comes from
  that timeline, never a fixed number.
- Each clip is an `<audio>` with `data-start` (its place on the timeline), `data-duration` (its
  length) and `data-media-start` (its `cut_in`). Never trim or stretch speech to fit a picture.
- `pause_after` is the script's pause; scale the pauses for the style (shorter for a fast cut,
  longer before a reveal). A line with `joined_next: true` runs into the next with no silence to
  cut in: keep the two back to back, with no pause. A line's `directions` (as whispering, laugh)
  say how it was voiced, for the picture to match; `unrecognized` lists the script's other
  bracketed asides, spoken as plain words.
- Captions are karaoke: each line's `words` carry `startMs` and `endMs` from the take's start, so
  move them onto the timeline, page them per line so a page never crosses lines, one page a clip
  at a time, the spoken word highlighted by the GSAP timeline. Words are inline-block spans with
  white-space: pre, or their leading spaces vanish. Scaling the spoken word up covers the spaces
  beside it: pad it sideways by what the scale adds, or highlight by colour alone.
- Music: a bed from the world's assets, when it has one, under the whole video and ducked under
  speech with a `data-automation` volume lane (about 0.45 between lines, 0.12 while a line plays).
  No bed, no music. The bed plays once, not looped: pick or trim one at least as long as the film.
A sample, voice.mjs, writing a composition doing all this, to adapt to the script and the brief:
```js
// Voice first: writes $composition from timings.json, since HyperFrames reads clips, audio and the
// film's length from the HTML before any script runs. Expects timings.json and its takes
// (narration/*.wav) copied into the project, and a bed from the world's assets as bed.mp3 when
// there is one. Run `node voice.mjs` after every edit to it, then lint.
import { existsSync, readFileSync, writeFileSync } from "node:fs";

const timings = JSON.parse(readFileSync("timings.json", "utf8"));
const LEAD = 0.4; // seconds of picture before the first word
const PAUSE = 1; // scale the script's pauses for the style: under 1 snappier, over 1 calmer
const PAGE_MS = 1200; // a caption page holds the words spoken within this span of its first
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const sec = (n) => Number(n.toFixed(3));

const clips = [];
const pages = [];
let at = LEAD;
for (const take of timings.takes) {
  for (const line of take.lines) {
    const length = line.cut_out - line.cut_in;
    clips.push({ src: take.file, from: at, cutIn: line.cut_in, length });
    // Word times are ms from the take's start: move them onto the timeline, in seconds.
    const shift = at - line.cut_in;
    const words = line.words.map((w) => ({ text: w.text, at: w.startMs / 1000 + shift }));
    // One line at a time, so a page never crosses lines.
    for (const word of words) {
      const page = pages.at(-1);
      if (page && page.line === line && word.at - page.words[0].at < PAGE_MS / 1000) page.words.push(word);
      else pages.push({ line, words: [word] });
    }
    for (const [i, page] of pages.entries()) {
      if (page.line !== line) continue;
      const next = pages[i + 1];
      page.from = page.words[0].at;
      page.until = next ? next.words[0].at : at + length;
    }
    // A line joined to the next has no silence to cut in: keep the two back to back.
    at += length + (line.joined_next ? 0 : line.pause_after * PAUSE);
  }
}
const SECONDS = sec(at + 0.6);

// The bed ducks under speech: 0.45 between lines, 0.12 while one plays, 0.1 s ramps. Lines
// closer than two ramps (joined ones, short pauses) are one span, or the bed swells between them.
// ponytail: four points a span, so 128 spans at most (a lane holds 512); widen the merge past that.
const spans = [];
for (const c of clips) {
  const span = spans.at(-1);
  if (span && c.from - span.until <= 0.2) span.until = c.from + c.length;
  else spans.push({ from: c.from, until: c.from + c.length });
}
const duck = [{ t: 0, v: 0.45 }];
for (const s of spans) {
  duck.push({ t: sec(Math.max(0, s.from - 0.1)), v: 0.45 }, { t: sec(s.from), v: 0.12 });
  duck.push({ t: sec(s.until), v: 0.12 }, { t: sec(s.until + 0.1), v: 0.45 });
}
const lane = JSON.stringify({ version: 1, lanes: [{ target: "volume", points: duck }] });
const bed = existsSync("bed.mp3")
  ? `<audio id="bed" src="bed.mp3" data-start="0" data-duration="${SECONDS}" data-track-index="1" data-automation="${lane.replace(/"/g, "&quot;")}"></audio>`
  : "";

const voice = clips.map(
  (c, i) =>
    `<audio id="line-${i}" src="${c.src}" data-start="${sec(c.from)}" data-duration="${sec(c.length)}" data-media-start="${sec(c.cutIn)}" data-track-index="0"></audio>`,
);
// Captions are karaoke: one page at a time, its spoken word coloured. Words are inline-block
// spans with white-space: pre, or their leading spaces vanish.
const captions = pages.map(
  (p, i) =>
    `<p id="page-${i}" class="clip page" data-start="${sec(p.from)}" data-duration="${sec(p.until - p.from)}" data-track-index="2">` +
    p.words.map((w) => `<span class="word" data-at="${sec(w.at)}">${esc(w.text)}</span>`).join("") +
    "</p>",
);

writeFileSync(
  "$composition",
  `<!doctype html>
<html
  lang="en"
  data-composition-variables='[
    {"id": "script", "type": "string", "label": "Script", "default": ""},
    {"id": "scriptPath", "type": "string", "label": "Script path", "default": ""}
  ]'
>
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=1080, height=1920" />
    <script src="node_modules/gsap/dist/gsap.min.js"></script>
    <style>
      * { margin: 0; padding: 0; box-sizing: border-box; }
      html, body { width: 1080px; height: 1920px; overflow: hidden; background: #111; }
      #root { width: 100%; height: 100%; font-family: sans-serif; }
      .page {
        position: absolute; left: 0; right: 0; top: 860px; padding: 0 80px;
        text-align: center; color: #fff; font-size: 104px; font-weight: 900; line-height: 1.15;
      }
      .word { display: inline-block; white-space: pre; }
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="${SECONDS}" data-width="1080" data-height="1920">
      ${[bed, ...voice, ...captions].filter(Boolean).join("\n      ")}
    </div>
    <script>
      const tl = gsap.timeline({ paused: true });
      // Colour alone marks the spoken word: scaling it up would cover the spaces beside it.
      for (const word of document.querySelectorAll(".word")) {
        const page = word.closest(".page");
        const end = Number(page.dataset.start) + Number(page.dataset.duration);
        const next = word.nextElementSibling;
        tl.set(word, { color: "#FF6B35" }, Number(word.dataset.at));
        tl.set(word, { color: "#FFF" }, next ? Number(next.dataset.at) : end);
      }
      window.__timelines = window.__timelines || {};
      window.__timelines["main"] = tl;
    </script>
  </body>
</html>
`,
);
```
