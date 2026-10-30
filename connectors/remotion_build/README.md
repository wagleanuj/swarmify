# remotion_build

The Remotion build connector: one sandboxed coding session writes a Remotion project for the
run's script, then the remotion_render package's own render, not the agent, turns it into the
stage's video. It is a Connector Package (ADR 0019) and one of Swarmify's default packages, so
workflows call `remotion_build` exactly as before. Everything Swarmify knows of it is
`swarmify-connector.toml`: it requests a Build Session (see Build Sessions in Swarmify's README,
which covers the session, its sandbox, its budget and revisions after a send-back) and has no
server of its own.

What it gives the session, all files beside this one:

- `system.md`, the builder's system prompt: how to work in the seeded project, assets and fonts
  under `public/` because the render has no network, and how to keep a 3D scene
  (`@remotion/three`) deterministic.
- `brief.md`, the task: build composition `$composition` from the script at `$input`.
- `voice_first.md`, added when one of the stage's inputs is a `timings.json` (below).
- `starter/`, the minimal project a build starts from when it names no `base`: `src/index.ts`
  registering a Root with composition `Main`, and `@remotion/captions` among its dependencies.
- The network the session may reach (`permissions.network`): the npm registry and Google Fonts.

The render is remotion_render's `remotion_render` tool, called on the build folder with the
composition (`[build_session]` `connector`, `tool` and `arguments`), so a world enables both
(both are defaults). Sharing that code path, rather than a copy of it, keeps one render and one
Chrome Headless Shell (800 MB) for both.

## The stage

```yaml
  - connector: video
    calls: remotion_build
    input: script.md            # the script artifact an earlier stage produced
    # or inputs: [script.md, timings.json], the script first, the rest named to the builder
    output: video.mp4           # the default
    params:                     # all optional
      base: shorts              # a template in <data dir>/remotion/ to start from (a name, not a path)
      composition: Main         # the default
      brief: Bold captions, 20 seconds, a cold open.
      model: claude-sonnet-5-5  # over the world's worker model (the Cast), else the default
      max_turns: 80             # the default
      minutes: 20               # the session's time limit, the default
```

`base` copies a template by name from the data directory's `remotion/` folder instead of the
starter; the workflow check reports one that is not there.

When one of `inputs` is a `timings.json` (the speech package's), the brief turns voice
first, as the spike on branch `spike/voice-first-video` proved: copy the timings and their takes
into `public/`, take the composition's length from the voice timeline, play each line as a trimmed
`<Audio>` in a `<Sequence>` spaced by its pause (scaled for the style, none after a
`joined_next` line), page karaoke captions per line with `createTikTokStyleCaptions` and
`white-space: pre`, and duck a music bed from the world's assets under speech. `voice_first.md`
carries a sample composition doing all of it, which a test type-checks in the starter.

The render executes the agent's code (its `remotion.config.ts`, its `node_modules`), so a project
under the data directory's `worlds/` renders in an OS sandbox of its own: `sandbox-exec` on macOS,
`bwrap` on Linux, and no render where neither exists. It has no network beyond loopback (fonts and
images must be files in `public/`, which the builder is told), reads nothing of the data directory
but the project, the output folder and the shared browser, and writes only those and temp. It
never installs: a build that left no `node_modules` fails. Chrome Headless Shell is the one the
remotion_render package's install step downloaded beside it (else one on `PATH`). On macOS that
profile starts from allow-all and denies network, the data directory, writes, other processes,
Apple Events and Launch Services, since Chrome needs Mach services a deny-all profile refuses;
Linux unshares every namespace.

Tests drive the builder with the scripted CLI and the render with a fake `npx` inside the real
sandbox. One real build, on your subscription and the real Remotion CLI, runs when you opt in:
`SWARMIFY_REAL_BUILD=1 uv run pytest apps/api/tests/test_build_http.py -k real -s` (about a
minute on a Mac, most of it the first npm install and the Chrome download).

