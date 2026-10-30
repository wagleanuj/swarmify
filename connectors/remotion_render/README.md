# remotion_render

The Remotion render connector: our stdio MCP wrapper over `npx remotion render` (ADR 0015). It
renders a script artifact through a Remotion project's composition into a video. It is a
Connector Package (ADR 0019) and one of Swarmify's default packages: on startup Swarmify installs
it from this folder into `<data dir>/packages/remotion_render/<version>/` and enables it for
every world that has not decided otherwise, so workflows call `remotion_render` exactly as before.
Everything Swarmify knows of it is `swarmify-connector.toml`.

Its install step (ADR 0026) puts beside it its Python (`uv sync`: the MCP SDK), the Remotion CLI
(`npm ci` from `package-lock.json`) and the Chrome Headless Shell Remotion downloads for it
(`npx remotion browser ensure`), about 800 MB. Every render uses that browser, so a render needs
nothing of Swarmify's image but Node and, in Docker, Chrome's shared libraries. When it is
missing (an install from a copy without `node_modules`) a Chrome Headless Shell on `PATH` is used
instead, else Remotion finds or downloads its own for a template render.

## Setup

Two Settings on the Connectors page, shared or per world:

- **Data folder** (`data_folder`, reaching the connector as `REMOTION_DATA_DIR`), default `.`: a
  relative path is taken from Swarmify's data directory, so the default is that directory itself
  (`~/.swarmify` natively, `/data` in Docker). Its `remotion/` folder holds the templates a
  relative `project` names; a project under its `worlds/` folder was written by an agent and
  renders sandboxed (below). Leave it as is unless the templates live elsewhere.
- **Render concurrency** (`concurrency`, reaching it as `SWARMIFY_RENDER_CONCURRENCY`), empty by
  default: browser tabs rendering at once, as `2` or `50%`. Empty, Remotion picks half the CPUs;
  with no GPU (Docker) a 3D scene (`@remotion/three`) renders in software and that many run out
  of memory, so set 2 there. Upgrading from V1, the start that first installs the package copies a
  `SWARMIFY_RENDER_CONCURRENCY` set in the app's environment (V1's image set 2) into this Setting.

## The stage

```yaml
  - connector: render
    calls: remotion_render
    input: script.md          # the script artifact an earlier stage produced
    output: short.mp4
    params:
      project: shorts           # a template in <data folder>/remotion/, or a folder path
      composition: Short        # the composition ID
      # optional: entry (src/index.ts), codec (h264), frames (0-29), props
```

A relative `project` is a template by name under the data folder's `remotion/` folder, so a
workflow moves between hosts unchanged; an absolute or `~` path works too. A symlink there is
followed, so a template can stay in its own folder: `ln -s ~/videos/shorts ~/.swarmify/remotion/shorts`.
When the project has no `node_modules`, the first render runs `npm ci` (`npm install` without a
lockfile) in it, its output in the render log.

The workflow check reports a stage without an input, an output, a `project` or a `composition`
(from the Manifest), and, through the package's `check` tool (the `check` capability), a project
that is not there: `the Remotion project shorts does not exist in ~/.swarmify/remotion`.

## The render

The composition gets the script as input props, `{"script": <text>, "scriptPath": <path>, ...props}`
(read them with `getInputProps()`). `remotion_render` answers with a long job and runs the render
detached from the MCP session, so a restart of Swarmify resumes polling it with
`remotion_render_status`; progress comes from the CLI's `Rendered n/N` and `Encoded n/N` lines and
shows on the timeline. On success the video gets a sidecar, `short.mp4.md`, with frontmatter (type
video, author remotion_render, world, run, stage, created, source script, composition, bytes, and
the template's git commit when the project is a git checkout). On failure the stage fails with
the last 40 lines of CLI output. Every render serves its bundle on a port the OS just handed out
(`--port`), never Remotion's default 3000, where another server (an open-webui container, say)
would answer and fail the render with `window.getStaticCompositions is not defined`.

## In Docker

Swarmify's image carries Node and the shared libraries and fonts Chrome needs, not Remotion or
Chrome: this package's install step puts both in `/data/packages`. A template must be inside the
container: copy it to `/data/remotion` (`docker compose cp ./shorts api:/data/remotion/shorts`)
and the stage's `project: shorts` finds it, as natively. The first render installs its
dependencies. Set **Render concurrency** to 2 there (above); no `--gl` flag is passed: the default
renders 3D on the Mac and in the image, and `--gl=angle` crashes in the image.

## A project an agent wrote

A `remotion_build` stage renders the project its coding session wrote through this package. That
render executes the agent's code (its `remotion.config.ts`, its `node_modules`), so a project
under the data folder's `worlds/` renders in an OS sandbox of its own: `sandbox-exec` on macOS,
`bwrap` on Linux, and no render where neither exists. It has no network beyond loopback, reads
nothing of the data folder but the project, the output folder and this package's browser, and
writes only those and temp. It never installs: a build that left no `node_modules` fails, as does
one with no Chrome Headless Shell to hand it, since the sandbox cannot download one.

## Tests

`test_remotion_render.py` installs the package through the real install path (with the test
suite's `npm` and `npx` that only log), gives it to a world and renders over stdio MCP with a
fake Remotion CLI on `PATH` (`fake_npx.py`), the agent-written case inside the real sandbox. One
real render runs when you opt in:
`SWARMIFY_REMOTION_PROJECT=<project> SWARMIFY_REMOTION_COMPOSITION=<id> uv run pytest connectors/remotion_render -k real_render`.
