# blender_render

The Blender render connector: our stdio MCP server that renders a Blender project's shots on the
GPU, runs the project's own finish step and writes the stage's video (ADR 0015). It is a Connector
Package (ADR 0019) installed with Swarmify from this folder into
`<data dir>/packages/blender_render/<version>/`, but **not enabled by default**
(`enable_by_default = false`): a world enables it on the Connectors page, with `blender_build`,
when it makes videos in Blender. Everything Swarmify knows of it is `swarmify-connector.toml`.

Its install step (ADR 0026) puts beside it its Python (`uv sync`: the MCP SDK and Pillow, for
contact sheets), then runs `fetch.py` (as `npm run fetch`, so the test suite's npm, which only
logs, stands in for it):

- **Blender**: the Linux x64 tarball of each version `fetch.py` pins a SHA-256 for (5.2.1, from
  the `.sha256` file beside it on download.blender.org, which serves it directly, no mirror
  redirect), unpacked to `vendor/blender/<version>/`, about 380 MB. A download that does not match
  its pin is refused and removed. Off Linux x64 there is no tarball to unpack and a render uses a
  `blender` on `PATH`.
- **ffmpeg and ffprobe**, each download checked against a pinned SHA-256, into `vendor/bin/`. On
  Linux x64 they come from BtbN's FFmpeg-Builds GPL tarball of ffmpeg 8.1.3 (a month-end
  `autobuild-2026-10-31-12-59` release, which BtbN keeps; about 150 MB, only the two binaries
  unpacked): ffmpeg-static's `b6.1.1` there lacks `drawtext`, which ffmpeg 6.1 builds only with
  harfbuzz. Elsewhere they are ffmpeg-static's `b6.1.1` release builds (the ones
  `hyperframes_render` fetches); its macOS arm64 build has the same filters. On both a finish
  step gets the filters [the project contract](#the-project-contract) lists.

The install reaches only `download.blender.org`, `github.com` and
`release-assets.githubusercontent.com` besides uv's PyPI hosts; a render reaches no network.
Measured on the box (2026-11-06): the whole fetch took 19 s, before ffmpeg moved to BtbN's build.

## Setup

One Setting on the Connectors page, shared or per world:

- **Blender version** (`blender_version`, reaching it as `SWARMIFY_BLENDER_VERSION`), `5.2.1` by
  default: the Blender a render runs. Only a version `fetch.py` pins a checksum for is accepted
  (`fetch.py <version>` refuses any other before downloading, and a render refuses it too); to
  move versions, add its pin to `fetch.py` and release a new package version.

## The stage

```yaml
  - connector: video
    calls: blender_render
    output: video.mp4
    params:
      project: short            # one of the world's Blender Templates
      quality: draft            # optional: a preset of its render.json, draft by default
```

`project` is a Template by name (ADR 0028): a folder of the world's `assets/blender_templates/`,
the Owned Folder this package shares with `blender_build`, read in place and never written. A
Template needs a `render.json`. A `blender_build` stage renders its build folder through this
package instead.

## The project contract

A project is a folder with a `render.json` at its root:

```json
{
  "fps": 30, "width": 1080, "height": 1920,
  "shots": [{"id": "s01", "script": "shots/s01.py"}],
  "quality": {"draft": {}, "final": {}},
  "finish": ["python3", "tools/finish.py"]
}
```

- `fps`, `width`, `height` (required): what each shot's frames are encoded at (the join and the
  contact sheet use them too). The shot script owns its own render settings.
- `shots` (required): in order, each a plain `id` (letters, digits, `_`, `-`) and a Blender Python
  `script` inside the project.
- `quality` (optional): presets by name. The names are passed through to the shot (the default is
  `draft`); a render asking for a name the project does not list is refused. Without `quality` any
  plain name passes.
- `finish` (optional): a command (argv), run with the project as its working directory after all
  shots. It must write the output video.

Each shot runs as its own Blender process, holding the GPU lock:

```
blender -b --factory-startup -noaudio --python-exit-code 1 --python <script> -- <quality>
```

`--python-exit-code 1` makes a script that raises exit non-zero (Blender exits 0 otherwise). Its
environment:

| Variable | What |
| --- | --- |
| `BLENDER_PROJECT` | the project folder (absolute) |
| `BLENDER_SHOT` | the shot id |
| `BLENDER_SHOT_OUT` | the folder to write frames into |
| `BLENDER_SHOT_QUALITY` | the quality name (also the first argument after `--`) |
| `BLENDER_SHOT_FRAMES` | comma-separated frame numbers to render, empty for all |
| `BLENDER_ASSETS` | the world's Assets folder (read-only) |

A shot writes `frame_<NNNN>.png`, the frame number zero-padded to 4, at the project's full width
and height: `frame_0001.png` up for a full render, and with `BLENDER_SHOT_FRAMES` set exactly the
frames it lists (`frame_0012.png` for frame 12). The package encodes each shot's frames to
`<id>.mp4` at `fps`, `width` x `height` (H.264, yuv420p).

The finish command's environment:

| Variable | What |
| --- | --- |
| `BLENDER_PROJECT` | the project folder |
| `BLENDER_ASSETS` | the world's Assets folder (read-only) |
| `BLENDER_SHOTS` | the folder holding `<id>.mp4` per shot, encoded at `fps` |
| `BLENDER_RUN` | the run's folder: the speech stage's `timings.json` and narration are in its stage folders |
| `BLENDER_OUTPUT` | the video path to write |

with `ffmpeg` and `ffprobe` (the package's) and a Python 3 as `python3` (the
package's own uv Python, standard library only) first on `PATH`. A finish step should write only
`BLENDER_OUTPUT` and temporary files (`TMPDIR`), not into the project: a changed project file
counts as a shared file and re-renders every shot. Without `finish` the shots are joined in
order (ffmpeg's concat, streams copied).

The package's ffmpeg guarantees the `drawtext` filter (freetype, harfbuzz, fontconfig), the `ass`
and `subtitles` filters (libass) and the `libx264` encoder, on Linux x64 and macOS arm64. Other
platforms get ffmpeg-static's builds as they are.

## The render

`blender_render` answers with a long job and renders in a process of its own, which the stage
polls with `blender_render_status`. Progress shows shot n of m (the shots are the first 90
percent), "waiting for the GPU" while another GPU job holds the card, then "finishing". A
repeated call with the same idempotency key attaches to the running render. On success the video
gets a sidecar, `video.mp4.md` (type video, author blender_render, world, run, stage, project,
quality, bytes).

Its files, in the stage's folder (`<stage>`, beside `artifacts/` and a build's `build/`, so a
send-back's copy of the build does not copy frames):

- `frames/<id>/frame_NNNN.png` and `frames/<id>.stamp`: each shot's frames and what they were
  rendered from;
- `shots/<id>.mp4`: each shot's video;
- `shader-cache/`: NVIDIA's shader disk cache (below);
- `artifacts/.video.mp4.log`: the render's log, Blender's output without its per-frame `Fra:`
  status lines, and `artifacts/.video.mp4.job.json`, the job's state.

**Shot cache.** A shot's stamp hashes its script, its `render.json` entry, the quality and its
preset, `fps`, `width`, `height` and the project's shared files: every file but `render.json`,
the shot scripts, `*.pyc` and the folders `__pycache__`, `.git`, `node_modules`, `renders` and
`output`. A shot whose stamp is unchanged and whose frames and video are there is skipped; so a
second render with nothing changed renders nothing, changing one shot's script re-renders that
shot, and changing a shared file (a kit module, a `.blend`) re-renders all. The world's Assets
are not hashed: a changed rig there needs a changed project file (or a new stage folder) to
re-render.

**Failures are loud.** A shot whose Blender exits non-zero fails the stage with
`shot <id>: Blender exited with <code>`, one that writes no frames with `shot <id> wrote no
frames`, a finish that fails or writes nothing with `the finish step exited with <code>` or `the
finish step wrote no video at <path>`; each followed by the last 40 lines of the log. The finish
step's stdout and stderr go to the same log, after the shots'; when it fails, only its own last 40
lines follow, so its message leads the error rather than Blender's frame saves.

**Cancel.** SIGTERM to the job process kills the Blender (or ffmpeg, or finish) it is running and
fails the job with `the render was cancelled`. Aborting the run cancels it through
`blender_render_status` with `cancel: true`, which sends that SIGTERM and SIGKILLs the job's process
group if it is still there after 5 s. In Docker the job runs in the connector sandbox's
PID namespace, so when the stage's sandbox ends every process in it ends too.

## Stills

`stills` renders chosen frames of one shot and answers with one contact sheet: MCP image content
(a PNG) and the sheet's path as text. A Build Session calls it to check its work:
`blender_build` lends it to its session through `[build_session] tools = ["stills"]` (#403), so
the session calls `mcp__blender_render__stills`, its server confined to the build's stage.

| Argument | What |
| --- | --- |
| `project` | the project folder, a full path: the build folder (`<stage>/build`) |
| `shot` | a shot id of its `render.json` |
| `frames` | frame numbers from 1, 1 to 24 of them |
| `quality` | a preset of its `render.json`, `draft` by default |

The shot runs as in a render, with `BLENDER_SHOT_FRAMES` listing the frames (sorted, repeats
dropped) and `BLENDER_SHOT_OUT` the folder `<stage>/stills/<shot>/`, emptied first; the shot
writes `frame_<NNNN>.png` for each at full width and height. It holds the GPU lock like a render
and uses the stage's `shader-cache/`, so stills after the stage's first Blender process skip the
shader compile. The sheet, `<stage>/stills/<shot>.png`, tiles the frames in order, at most four a
row, each scaled to 480 pixels on its longer side (`width` x `height` of `render.json`) under a
label naming the shot and frame (`s01 frame 12`); Blender's log is `<stage>/stills/<shot>.log`.

It answers with an error, never an empty sheet, for an unknown shot (naming the project's shots),
a shot whose Blender exits non-zero, writes no frames or misses some of those asked (naming them),
each with the last 40 lines of its log; and for no frames, more than 24, or a quality the project
does not offer. Measured on the box (2026-11-06): eight 1080x1920 EEVEE stills took 15 s cold
(the shader compile) and 4.3 s warm.

## GPU

`permissions.gpu = true`: the sandbox binds the host's GPU device files and hands the process
`SWARMIFY_GPU_LOCK`, the lock file every GPU-granted process shares. Each Blender process holds an
exclusive `flock` on it while it runs (non-blocking first, logging `Waiting for the GPU`, then
blocking), so a render, a build's stills and a Remotion GPU render never share the card at once.
Without the variable it runs unlocked.

Blender needs nothing more on the GPU: with the sandbox's binds its `gpu.platform` reports NVIDIA
(no `__EGL_VENDOR_LIBRARY_*` variables). A cold Blender process spends about 11 s compiling
shaders into NVIDIA's disk cache; `__GL_SHADER_DISK_CACHE_PATH` points it at the stage's
`shader-cache/` (with `__GL_SHADER_DISK_CACHE_SKIP_CLEANUP=1`), so only a stage's first process
pays it. Measured on the box (RTX 2070 Super, driver 560, 2026-11-06): two 60-frame 1080x1920
EEVEE shots (a default scene, default samples) rendered in 34 s cold and 24 s warm; a re-render
with nothing changed took 0.6 s; changing one shot re-rendered only it.

Without a GPU (no `/dev/nvidia0` or `/dev/dri/renderD*`) the render still runs, on the CPU, and
logs `No GPU here: Blender renders on the CPU, slowly` (the spike measured 164 s for 60 frames
against 10.7 s on the GPU).

## Tests

`test_blender_render.py` installs the package through the real install path (with the suite's
npm, which only logs), enables it for a world and renders over stdio MCP in the real connector
sandbox, and in process, with a fake Blender and ffmpeg on `PATH` (`fake_blender.py`,
`fake_ffmpeg.py`): shot order and environment, the join, the finish environment, the shot cache,
failures, progress, cancel and the GPU lock with two renders at once; and `stills` over the
server a Build Session is given, confined to its stage: the sheet, its labels and its errors. With a real `blender` and
`ffmpeg` on `PATH`, `test_a_real_blender_renders_a_3_frame_shot` renders a 3-frame shot. After
`npm run fetch` (`vendor/bin/ffmpeg` present),
`test_the_fetched_ffmpeg_has_the_filters_a_finish_step_is_promised` checks those filters and
libx264.
