# hyperframes_build

The HyperFrames build connector: one sandboxed coding session writes a HyperFrames project (HTML,
CSS and a GSAP timeline) for the run's script, then the hyperframes_render package's own render,
not the agent, turns it into the stage's video. It is a Connector Package (ADR 0019) installed
with Swarmify from this folder but **not enabled by default** (`enable_by_default = false`):
Remotion stays a world's video engine until its operator enables this package and
hyperframes_render on the Connectors page. Everything Swarmify knows of it is
`swarmify-connector.toml`: it requests a Build Session (see Build Sessions in Swarmify's README,
which covers the session, its sandbox, its budget and revisions after a send-back) and has no
server of its own.

What it gives the session, all files beside this one:

- `system.md`, the builder's system prompt: HyperFrames' timed clips and its one paused GSAP
  timeline, reading the script with `getVariables()`, every script, font and image a file in the
  project because the render has no network (fonts through HyperFrames' own localizer), and
  `lint`, `check` and `snapshot` before it finishes.
- `brief.md`, the task: build composition `$composition` from the script at `$input`.
- `starter/`, the project a build starts from when it names no `base`: HyperFrames' blank project
  (0.9.0) with the CLI pinned in `devDependencies`, GSAP from npm instead of a CDN, no web font,
  and an `index.html` showing the script's first line. Its npm scripts (`lint`, `check`,
  `snapshot`, `fonts`) run HyperFrames with its telemetry and update check off and its cache in
  the project's `.hyperframes/`, so the builder never reaches beyond its sandbox by accident.
- The network the session may reach (`permissions.network`): the npm registry, Google Fonts and
  jsDelivr, for the builder to download what the project uses into it.

The render is hyperframes_render's `hyperframes_render` tool, called on the build folder with the
composition (`[build_session]` `connector`, `tool` and `arguments`), so a world enables both.
Sharing that code path, rather than a copy of it, keeps one render and one Chrome Headless Shell
for both.

## The stage

```yaml
  - connector: video
    calls: hyperframes_build
    input: script.md            # the script artifact an earlier stage produced
    # or inputs: [script.md, notes.md], the script first, the rest named to the builder
    output: video.mp4           # the default
    params:                     # all optional
      base: promo               # a template in <data dir>/hyperframes/ to start from (a name, not a path)
      composition: index.html   # the default
      brief: Bold captions, 20 seconds, a cold open.
      model: claude-sonnet-5-5  # over the world's worker model (the Cast), else the default
      max_turns: 80             # the default
      minutes: 20               # the session's time limit, the default
```

`base` copies a template by name from the data directory's `hyperframes/` folder instead of the
starter; the workflow check reports one that is not there.

The render executes the agent's code in the hyperframes_render package's connector sandbox (ADR
0021, its README): no network beyond its own loopback, nothing of the data directory readable but
that package, the templates and the run's folders, and nothing writable but the stage's folder.
It never installs: a build that left no `node_modules` fails. So the builder is told to keep
`node_modules`, load GSAP from it, and vendor every font and image.

`check` and `snapshot` need a Chrome Headless Shell. The session cannot reach
hyperframes_render's (it is in the packages folder, which the session may not read), so they use
one HyperFrames finds on the host; where there is none, the builder says so and relies on `lint`,
which needs no browser.

## What the first real render of the starter settled (2026-11-02, macOS arm64, Node 22.23.4)

The starter, its `node_modules` installed, renders through hyperframes_render's real install in
its sandbox with no network: GSAP loads from `node_modules`, the script's first line shows from
the variables, and the compiler's fetch of Google Fonts for `sans-serif` is refused, logged as
blocked, and falls back to the Inter HyperFrames bundles.

Tests drive the builder with the scripted CLI and the render with a fake HyperFrames CLI inside
the real connector sandbox; `test_hyperframes_build.py` runs HyperFrames' own linter on the
starter when npm is here. One real build, on your subscription and the real HyperFrames CLI, runs
when you opt in:
`SWARMIFY_REAL_BUILD=1 uv run pytest apps/api/tests/test_build_http.py -k real_hyperframes -s`.
