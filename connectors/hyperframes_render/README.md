# hyperframes_render

The HyperFrames render connector: our stdio MCP wrapper over `hyperframes render` (ADR 0015). It
renders a script artifact through a HyperFrames project (HTML, CSS and a GSAP timeline) into a
video. It is a Connector Package (ADR 0019) installed with Swarmify from this folder into
`<data dir>/packages/hyperframes_render/<version>/`, but **not enabled by default**
(`enable_by_default = false`): Remotion stays a world's video engine until its operator enables
this package on the Connectors page. Everything Swarmify knows of it is `swarmify-connector.toml`.

Its install step (ADR 0026) puts beside it its Python (`uv sync`: the MCP SDK), the HyperFrames
CLI (`npm ci` from `package-lock.json`, 0.9.0, which needs Node 22), ffmpeg and ffprobe
(`npm run ffmpeg`, below) and the Chrome Headless Shell HyperFrames downloads for it
(`hyperframes browser ensure --force`, into `node_modules/.hyperframes`), about 450 MB, reaching
only its Manifest's `install_network`. Every render uses that browser and those binaries, so a
render needs nothing of Swarmify's image but Node and, in Docker, Chrome's shared libraries. When
one is missing (an install from a copy without `node_modules`) a `chrome-headless-shell`, `ffmpeg`
or `ffprobe` on `PATH` is used instead, else the render fails saying to reinstall the package: a
render cannot download one.

## Setup

Two Settings on the Connectors page, shared or per world:

- **Templates folder** (`templates`, reaching the connector as `HYPERFRAMES_TEMPLATES`), default
  `hyperframes`: a relative path is taken from Swarmify's data directory, so by default it is
  `~/.swarmify/hyperframes` natively and `/data/hyperframes` in Docker, beside the Remotion
  templates. It holds the templates a relative `project` names, and the connector sandbox lets the
  render read it (and the folders the links in it lead to), never write it.
- **Render workers** (`workers`, reaching it as `SWARMIFY_HYPERFRAMES_WORKERS`), empty by default:
  Chrome processes rendering at once (`--workers`), as `2` or `auto`. Empty, HyperFrames picks from
  the CPUs and memory; each worker is a Chrome of about 256 MB, so on a small Docker VM without a
  GPU set 1 or 2.

## The stage

```yaml
  - connector: video
    calls: hyperframes_render
    input: script.md          # the script artifact an earlier stage produced
    output: video.mp4
    params:
      project: promo            # a template in the templates folder
      # optional: composition (index.html), quality (draft, looks, delivery, standard, high),
      # fps (30, or 30000/1001), variables
```

A relative `project` is a template by name in the templates folder, so a workflow moves between
hosts unchanged; an absolute path works when it is in that folder, or in the run's folders (a
build's). A symlink there is followed, so a template can stay in its own repo:
`ln -s ~/videos/promo ~/.swarmify/hyperframes/promo`. A render runs the project's own HyperFrames
CLI, `node_modules/.bin/hyperframes`, and never installs (the sandbox gives it no network, and a
project's scripts would run with it): run `npm ci` in a template once. A project without
`node_modules` is refused with "run npm ci there".

The workflow check reports a stage without an input, an output or a `project` (from the Manifest),
and, through the package's `check` tool (the `check` capability), a project that is not there:
`the HyperFrames project promo does not exist in ~/.swarmify/hyperframes`. A check that cannot run
is a problem too.

## The render

The composition gets the script as variables, `{...variables, "script": <text>, "scriptPath":
<path>}`, through a variables file (`--variables-file`); read them with
`window.__hyperframes.getVariables()`. `hyperframes_render` answers with a long job and runs the
render in a process of its own, which the stage polls with `hyperframes_render_status` in the same
session, and which outlives a restart of Swarmify, whose stage then resumes polling it. A repeated
call with the same idempotency key attaches to the running render. Progress comes from the CLI's
`@hf-progress` lines and shows on the timeline as a percentage and a phase (compiling, capturing
frames 120/300, encoding). On success the video gets a sidecar, `video.mp4.md`, with frontmatter
(type video, author hyperframes_render, world, run, stage, created, source script, composition,
bytes, and the project's git commit when it is a git checkout). On failure the stage fails with
the last 40 lines of CLI output; a render that exits cleanly but writes no video fails too.

Every render runs with HyperFrames' telemetry, update and skills checks, self-installs and its
look for a studio on port 5190 off (`HYPERFRAMES_NO_TELEMETRY`, `DO_NOT_TRACK`,
`HYPERFRAMES_NO_UPDATE_CHECK`, `HYPERFRAMES_NO_AUTO_INSTALL`, `HYPERFRAMES_NO_PREVIEW_DISCOVERY`),
its cache (`HYPERFRAMES_CACHE_DIR`: fonts, extracted frames) in its own scratch folder, and
`HYPERFRAMES_BROWSER_PATH`, `HYPERFRAMES_FFMPEG_PATH` and `HYPERFRAMES_FFPROBE_PATH` pointing at
the package's own Chrome and binaries.

## Offline renders

A render has no network, so a project must carry every script, font and image as files: GSAP from
the project's `node_modules` (`<script src="node_modules/gsap/dist/gsap.min.js">`), never a CDN.
HyperFrames' compiler fetches Google Fonts for the families a composition names; offline that
fetch is refused (and logged as blocked) and it falls back to the fonts it bundles (Inter for a
generic `sans-serif`). A project that wants another face vendors it, with
`hyperframes-localize-fonts`.

## What the first real render settled (2026-11-02, macOS arm64, Node 22.23.4)

- **ffmpeg**: HyperFrames 0.9.0 sets no minimum version but passes `-fps_mode` (ffmpeg 5.1 and
  later) when it encodes and snapshots, so the script-free `@ffmpeg-installer` builds (ffmpeg 4.x)
  are too old. ffmpeg-static's builds are current (6.x), but its install script drops the sandbox
  proxy's credentials and fails with HTTP 407, and ffprobe-static carries no Linux arm64 build. So
  `ffmpeg.mjs` (`npm run ffmpeg`) fetches ffmpeg and ffprobe from ffmpeg-static's release `b6.1.1`
  itself, with Node's `fetch` (which goes through the proxy), and checks each against a pinned
  SHA-256. `npm ci` runs with `--ignore-scripts`; no package's install script runs.
- **Port**: none to choose. HyperFrames serves the project from its own file server on
  `127.0.0.1` port 0, a port the OS hands out, and has no `--port` flag; unlike Remotion's 3000
  nothing can already hold it.
- **install_network**: `registry.npmjs.org` (the CLI), `github.com` and
  `release-assets.githubusercontent.com` (ffmpeg and ffprobe), `storage.googleapis.com` (Chrome
  Headless Shell), beside uv's PyPI hosts. Each was confirmed by the real install in the sandbox,
  the Chrome host by an install that failed without it.

A render still tries `fonts.googleapis.com` (above) and writes HyperFrames' post-render state into
the project's `.hyperframes/`, which a read-only project refuses; both are logged as blocked and
neither fails the render.

## In Docker

Swarmify's image carries Node 22 and the shared libraries and fonts Chrome needs, not HyperFrames,
Chrome or ffmpeg: this package's install step puts them in `/data/packages`. Renders run in the
`sandbox` service (Swarmify's README, Deployment), which mounts the volume's `hyperframes/` folder.
A template must be there: copy it in and install its dependencies once,
`docker compose cp ./promo api:/data/hyperframes/promo` then
`docker compose exec -w /data/hyperframes/promo api npm ci`, and the stage's `project: promo` finds
it, as natively. Set **Render workers** to 1 or 2 on a small Docker Desktop VM (above). On Linux
arm64 HyperFrames has no Chrome Headless Shell to download (it would `apt-get install chromium`),
so there the install step skips it and a render needs a `chrome-headless-shell` on `PATH`, else it
fails saying to reinstall; a Linux x64 host is fine.

## A project an agent wrote

A project a build stage wrote, in the run's folders, renders through this package. That render
executes the agent's code, and it does so in this package's connector sandbox (ADR 0021): no
network beyond its own loopback, nothing of the data directory readable but this package, the
templates and the run's folders, nothing writable but its stage's folder and its scratch folder,
and no other package's keys (it has none of its own).

## Tests

`test_hyperframes_render.py` installs the package through the real install path (with the test
suite's `npm` and `npx` that only log), enables it for a world, runs the workflow check through
its `check` tool and renders over stdio MCP with a fake HyperFrames CLI as the project's own
(`fake_hyperframes.py`), a template in the templates folder or a run's build folder, all in the
real connector sandbox, where an agent-written project trying to read the data directory's keys,
write outside its stage and reach the internet is refused each time and each block logged. One
real install and render runs when you opt in, with a HyperFrames project whose `node_modules` are
installed (it is copied into a run's folder, where a build would have written it):
`SWARMIFY_REAL_BUILD=1 SWARMIFY_HYPERFRAMES_PROJECT=<project> uv run pytest connectors/hyperframes_render -k real_render -s`.
