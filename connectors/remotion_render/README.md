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

Two Settings on the Connectors page, shared or per world:

- **Templates folder** (`templates`, reaching the connector as `REMOTION_TEMPLATES`), default
  `remotion`: a relative path is taken from Swarmify's data directory, so by default it is
  `~/.swarmify/remotion` natively and `/data/remotion` in Docker. It holds the templates a
  relative `project` names, and the connector sandbox lets the render read it (and the folders the
  links in it lead to), never write it. It replaces V2's earlier **Data folder**.
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
      project: shorts           # a template in the templates folder
      composition: Short        # the composition ID
      # optional: entry (src/index.ts), codec (h264), frames (0-29), props
```

A relative `project` is a template by name in the templates folder, so a workflow moves between
hosts unchanged; an absolute path works when it is in that folder, or in the run's folders (a
build's). A symlink there is followed, so a template can stay in its own folder:
`ln -s ~/videos/shorts ~/.swarmify/remotion/shorts`. A render runs the project's own Remotion CLI,
`node_modules/.bin/remotion`, and never installs (the sandbox gives it no network, and a
template's scripts would run with it): run `npm ci` in a template once, and keep the fonts and
images it uses in it, as files, since a render cannot fetch them either.

The workflow check reports a stage without an input, an output, a `project` or a `composition`
(from the Manifest), and, through the package's `check` tool (the `check` capability), a project
that is not there: `the Remotion project shorts does not exist in ~/.swarmify/remotion`.

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

## In Docker

Swarmify's image carries Node and the shared libraries and fonts Chrome needs, not Remotion or
Chrome: this package's install step puts both in `/data/packages`. Renders run in the `sandbox`
service (Swarmify's README, Deployment), which mounts the volume's `remotion/` folder. A template
must be there: copy it in and install its dependencies once,
`docker compose cp ./shorts api:/data/remotion/shorts` then
`docker compose exec -w /data/remotion/shorts api npm ci`, and the stage's `project: shorts` finds
it, as natively. Set **Render concurrency** to 2 there, or 1 on a small Docker Desktop VM (above);
no `--gl` flag is passed: the default renders 3D on the Mac and in the image, and `--gl=angle`
crashes in the image.

## A project an agent wrote

A `remotion_build` stage renders the project its coding session wrote, in the stage's `build`
folder, through this package. That render executes the agent's code (its `remotion.config.ts`,
its `node_modules`), and it does so in this package's connector sandbox (ADR 0021): no network
beyond its own loopback, nothing of the data directory readable but this package, the templates
and the run's folders, nothing writable but its stage's folder and its scratch folder, and no
other package's keys (it has none of its own). A build that left no `node_modules` fails; it never
installs.

## Tests

`test_remotion_render.py` installs the package through the real install path (with the test
suite's `npm` and `npx` that only log), gives it to a world and renders over stdio MCP with a
fake Remotion CLI on `PATH` (`fake_npx.py`), all in the real connector sandbox, where an
agent-written project trying to read the data directory's keys, write outside its stage and reach
the internet is refused each time and each block logged. One real render runs when you opt in:
`SWARMIFY_REMOTION_PROJECT=<project> SWARMIFY_REMOTION_COMPOSITION=<id> uv run pytest connectors/remotion_render -k real_render`.
