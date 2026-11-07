# remotion_render

The Remotion render connector: our stdio MCP wrapper over `remotion render` (ADR 0015). It
renders a script artifact through a Remotion project's composition into a video. It is a
Connector Package (ADR 0019) and one of Swarmify's default packages: on startup Swarmify installs
it from this folder into `<data dir>/packages/remotion_render/<version>/` and enables it for
every world that has not decided otherwise, so workflows call `remotion_render` exactly as before.
Everything Swarmify knows of it is `swarmify-connector.toml`.

Its install step (ADR 0026) puts beside it its Python (`uv sync`: the MCP SDK), the Remotion CLI
(`npm ci` from `package-lock.json`) and the Chrome Headless Shell Remotion downloads for it
(`npx remotion browser ensure`), about 800 MB, reaching only npm and Remotion's host for the
browser (Google's storage where Remotion's own build does not fit the machine), its Manifest's
`install_network`. Every render uses that browser, so a render needs nothing of Swarmify's image
but Node and, in Docker, Chrome's shared libraries. When it is missing (an install from a copy
without `node_modules`) a Chrome Headless Shell on `PATH` is used instead, else the render fails
saying to reinstall the package: a render cannot download one.

## Setup

One Setting on the Connectors page, shared or per world:

- **Render concurrency** (`concurrency`, reaching it as `SWARMIFY_RENDER_CONCURRENCY`), empty by
  default: browser tabs rendering at once, as `2` or `50%`. Empty, Remotion picks half the CPUs;
  with no GPU (Docker) a 3D scene (`@remotion/three`) renders in software and that many run out
  of memory, so set 2 there. On a GPU a render always uses one tab and this Setting does not
  apply: tabs sharing the GPU sometimes captured a frame of a new 3D canvas before it was drawn
  (a black or garbled frame at random cuts), and one tab renders cleanly at about 6 frames a
  second, in chunks of 600 frames (The render, below). Upgrading from V1, the start that first installs the package copies a
  `SWARMIFY_RENDER_CONCURRENCY` set in the app's environment (V1's image set 2) into this Setting.

## The stage

```yaml
  - connector: render
    calls: remotion_render
    input: script.md          # the script artifact an earlier stage produced
    output: short.mp4
    params:
      project: shorts           # one of the world's Remotion Templates
      composition: Short        # the composition ID
      # optional: entry (src/index.ts), codec (h264), frames (0-29), props
```

`project` is a Template by name (ADR 0028): a folder of the world's `assets/remotion_templates/`,
the Owned Folder this package shares with `remotion_build`, so a Template one makes the other
renders. Add one on the world's Assets page by uploading a zip (or Save as Template on a finished
build); the core runs `npm ci` in it once, with only npm's registry reachable, and a stage
rendering it while that runs, or after it failed, is refused saying so. The core resolves the
name to the Template's folder and records the Template's name and fingerprint on the stage's
call. A render runs the project's own Remotion CLI, `node_modules/.bin/remotion`, and never
installs (the sandbox gives it no network, and a Template's scripts would run with it): keep the
fonts and images it uses in it, as files, since a render cannot fetch them either.

The workflow check reports a stage without an input, an output, a `project` or a `composition`
(from the Manifest), and a `project` the world has no Template of:
`the Template shorts is not one of this world's Remotion render Templates`.

## The render

The composition gets the script as input props, `{"script": <text>, "scriptPath": <path>, ...props}`
(read them with `getInputProps()`). `remotion_render` answers with a long job and runs the render
in a process of its own, which the stage polls with `remotion_render_status` in the same session,
and which outlives a restart of Swarmify, whose stage then resumes polling it; progress comes from the CLI's `Rendered n/N` and `Encoded n/N` lines and
shows on the timeline. On success the video gets a sidecar, `short.mp4.md`, with frontmatter (type
video, author remotion_render, world, run, stage, created, source script, composition, bytes, and
the template's git commit when the project is a git checkout). On failure the stage fails with
the last 40 lines of CLI output. Every render serves its bundle on a port the OS just handed out
(`--port`), never Remotion's default 3000, where another server (an open-webui container, say)
would answer and fail the render with `window.getStaticCompositions is not defined`.

On a GPU an h264 or h265 render runs as steps, each a fresh process of the project's CLI, since
one long-lived tab runs out of WebGL contexts (a project mounting a new 3D canvas per shot failed
at frame 1661 with `Error creating WebGL context`):

1. `remotion bundle <entry> --out-dir=.short.mp4.bundle`, once, which every later step renders from.
2. `remotion compositions .short.mp4.bundle --props=...` for the composition's frame count, read
   from its table (the CLI has no JSON output), unless `frames` gives the range.
3. `remotion render .short.mp4.bundle <composition> .short.mp4.parts/<n>.mp4 --frames=a-b
   --separate-audio-to=.short.mp4.parts/<n>.wav` for each 600 frames, one tab each
   (`--concurrency=1`), with the same codec, props and browser. Each chunk writes its own audio,
   silent when the composition has none, so no process walks the whole length (an audio-only
   `--codec=aac` render still renders every frame in the browser).
4. `remotion ffmpeg` joins the parts and the audio with the concat demuxer, the video copied and
   the audio encoded once to AAC at 320k, into `short.mp4`.
5. The hidden bundle and parts are deleted, whether it succeeded or not.

Each chunk's render holds the shared GPU lock (`SWARMIFY_GPU_LOCK`, see the connector sandbox
in Swarmify's README) while it runs, so another GPU job never draws on the card at the same time;
while one does, the render waits and its progress says `waiting for the GPU`. Between chunks the
lock is let go, so other jobs take turns with a long render.

Progress counts frames done over the whole range across chunks; a failing step fails the stage
with its name (`remotion render of frames 600-1199 exited with 1`) and the log's last 40 lines,
which hold every step's output. A GPU render therefore always has an audio track. Other codecs,
and every render in software, stay one `remotion render`.

## In Docker

Swarmify's image carries Node and the shared libraries and fonts Chrome needs, not Remotion or
Chrome: this package's install step puts both in `/data/packages`. Renders run in the `sandbox`
service (Swarmify's README, Deployment), which mounts the volume's `worlds/` folder, where each
world's Templates are, so an uploaded Template renders there as natively. Set **Render concurrency** to 2 there, or 1 on a small Docker Desktop VM (above);
no `--gl` flag is passed: the default renders 3D on the Mac and in the image, and `--gl=angle`
crashes in the image.

## A project an agent wrote

A `remotion_build` stage renders the project its coding session wrote, in the stage's `build`
folder, through this package. That render executes the agent's code (its `remotion.config.ts`,
its `node_modules`), and it does so in this package's connector sandbox (ADR 0021): no network
beyond its own loopback, nothing of the data directory readable but this package, the world's
Remotion Templates and the run's folders, nothing writable but its stage's folder and its scratch folder, and no
other package's keys (it has none of its own). A build that left no `node_modules` fails; it never
installs.

## Tests

`test_remotion_render.py` installs the package through the real install path (with the test
suite's `npm` and `npx` that only log), gives it to a world and renders over stdio MCP with a
fake Remotion CLI on `PATH` (`fake_npx.py`), all in the real connector sandbox, where an
agent-written project trying to read the data directory's keys or the world's other assets, write
outside its stage or into a Template and reach the internet is refused each time and each block
logged. One real render runs when you opt in:
`SWARMIFY_REMOTION_PROJECT=<project> SWARMIFY_REMOTION_COMPOSITION=<id> uv run pytest connectors/remotion_render -k real_render`,
the project copied into a world's Templates first.
