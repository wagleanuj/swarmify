# blender_build

The Blender build connector: one sandboxed coding session writes a Blender project for the run's
script, checking each shot with stills, then the blender_render package's own render, not the
agent, turns it into the stage's video on the GPU. It is a Connector Package (ADR 0019) installed
with Swarmify from this folder but **not enabled by default** (`enable_by_default = false`): a
world enables it on the Connectors page together with `blender_render`, which a build renders
through. Everything Swarmify knows of it is `swarmify-connector.toml`: it requests a Build
Session (see Build Sessions in Swarmify's README, which covers the session, its sandbox, its
budget and revisions after a send-back) and has no server of its own.

What it gives the session, all files beside this one:

- `system.md`, the builder's system prompt: the project contract, write shot scripts against the
  Template's kit as its guide says, keep `render.json` current, check every shot with `stills`
  (first, key and last frames) and read the contact sheet before finishing, and never run
  Blender, the full render or the finish step. Blender is not in the session's Bash (no binary,
  no GPU): `stills` is its only way to see a frame.
- `brief.md`, the task: build the project in the build folder from the script at `$input`,
  rendered at `$quality`.
- `voice_first.md`, added when one of the stage's inputs is a `timings.json` (below).
- `starter/`, the project a build starts from when it names no `base`: one shot, `s01`, a lit
  cube turning in front of a camera written with bpy alone, and a `render.json` (1080x1920 at
  30 fps, presets `draft` and `final`, no finish). It renders as is.
- No network: the session reaches none, so a project uses only what is in it or the world's
  Assets.

The session is offered blender_render's `stills` (`[build_session] tools = ["stills"]`, allowed as
`mcp__blender_render__stills`), its server confined to the stage: each sheet lands in
`<stage>/stills/<shot>.png`, outside `build/`. After the session the core calls blender_render's
`blender_render` on the build folder (`project = "$folder"`) with the stage's `quality`. Both
packages must be enabled for the world, or the stage fails before the session.

## The stage

```yaml
  - connector: video
    calls: blender_build
    inputs: [script.md, timings.json]  # the script first; speech's timings make it voice first
    output: video.mp4                  # the default
    params:                            # all optional
      base: kit                        # one of the world's Blender Templates (a name)
      quality: draft                   # a preset of its render.json, draft by default
      brief: Three shots, slow push-ins, 25 seconds.
      model: claude-sonnet-5-5         # over the world's worker model (the Cast), else the default
      max_turns: 80                    # the default
      minutes: 30                      # optional: a time limit in minutes; none by default
      tools: [motion_search]           # optional: packages of the world the session may also call
```

`base` copies a Template by name from the world's `assets/blender_templates/` folder (ADR 0028,
on the world's Assets page), the Owned Folder this package shares with `blender_render`, instead
of the starter. The workflow check reports one the world does not have. The stage's call records
the Template's name and fingerprint, and when the Template has a `TEMPLATE.md` the brief tells
the builder to read it first.

When one of `inputs` is a `timings.json` (the speech package's), the brief turns voice first: it
names the timings' absolute path and the takes' folder beside it (`narration/`), to read in
place, not copy, since the render reads the run's folders and a finish step finds them through
`BLENDER_RUN`; a Template's tool that times shots to the voice takes that path. Shots are cut to
the voice timeline, and the finish step lays the takes under the joined shots (the builder writes
one when the project has none).

## Making a Blender Template

A Template is a project that already renders: put a folder in the world's
`assets/blender_templates/<name>/` (or save a finished build as one from its run page). It needs:

- `render.json` at its root (required), per the project contract below.
- `TEMPLATE.md` (recommended), the guide the builder reads first: what the kit offers (sets,
  characters, cameras, a clock that times shots to the voice), how a shot script uses it, and
  what a shot must never do. Short and concrete; the builder follows it over its defaults.
- A kit: Python modules the shot scripts import (from `BLENDER_PROJECT`), `.blend` files they
  append from, and a finish step (`tools/finish.py` as `["python3", "tools/finish.py"]`) that
  cuts, captions and mixes. Large shared files (rigs, motion clips, fonts) can stay in the
  world's Assets, read through `BLENDER_ASSETS`.
- One or two example shots, so the builder copies a working pattern.

Saving a build as a Template drops `renders/` and `output/` (`[templates] strip`), which
blender_render never counts as project files; frames, shot videos and stills live in the stage
folder, outside `build/`, so none should be there.

## The project contract

blender_render's README holds it in full (`../blender_render/README.md`, The project contract).
In short: `render.json` lists `fps`, `width`, `height`, the `shots` in order (an `id` and a
`script` each), optional `quality` presets and an optional `finish` command. Each shot runs as
`blender -b --factory-startup -noaudio --python-exit-code 1 --python <script> -- <quality>` with
`BLENDER_PROJECT`, `BLENDER_SHOT`, `BLENDER_SHOT_OUT`, `BLENDER_SHOT_QUALITY`,
`BLENDER_SHOT_FRAMES` (empty for all frames) and `BLENDER_ASSETS`, and writes `frame_NNNN.png` at
full size into `BLENDER_SHOT_OUT`: every frame, or exactly those `BLENDER_SHOT_FRAMES` lists (how
`stills` renders a few). `starter/shots/s01.py` is the smallest script honouring all of it.

## Tests

`test_blender_build.py` checks the Manifest against blender_render's (one Templates folder, the
same quality default), the brief, the voice-first note and the system prompt; with a real
`blender` and `ffmpeg` on `PATH`, `test_the_starter_renders_as_is_with_real_blender` renders the
starter through blender_render's own job into a 60-frame 1080x1920 video (about 15 s on a Mac).
`apps/api/tests/test_build_http.py` runs a build stage end to end with the scripted CLI as the
builder and blender_render's fake Blender and ffmpeg in the real connector sandbox: seeded from
the starter or a Template, offered `stills` but never the render, rendered at the stage's
quality, and a send-back revising the kept build.
