# speech

The speech connector: it voices a script, one take per scene, and writes the word timings and cut
points a voice-first video is edited on. It is a Connector Package (ADR 0019) and one of
Swarmify's default packages: on startup Swarmify installs it from this folder into
`<data dir>/packages/speech/<version>/`, where its install step (`uv sync`) puts httpx and the MCP
SDK beside it (ADR 0026), and enables it for every world that has not decided otherwise, new worlds
included, so workflows call `speech` exactly as before. Everything Swarmify knows of it is
`swarmify-connector.toml`.

## Setup

One Setting, **Speech engines** (`speech_endpoints`, reaching the connector as
`SPEECH_ENDPOINTS`): the engines' URLs, comma separated, in the order to try them. It defaults to
`http://localhost:8881,http://localhost:8880`, the engines started natively; set it on the
Connectors page, shared by every world or per world.

Upgrading from V1, where it was an environment variable of the app: the start that first installs
the package copies a `SPEECH_ENDPOINTS` set in the app's environment into this shared Setting, so
a Docker deployment whose `.env` set it keeps its engines. After that the Setting is what counts.

The Manifest declares the engines as a Service (`[[services]]`, `engines`, its URLs from the
Setting), so the Connectors page shows whether each answers `GET /health`. Swarmify never starts
or stops them (ADR 0026): you run them, as below.

## Running the engines beside Swarmify

`compose.yaml` here holds the engines: Kokoro always, Chatterbox on the CPU under the
`chatterbox` profile, Chatterbox on an NVIDIA GPU under `chatterbox-gpu`. Start Swarmify first
(its compose file names its network `swarmify`, which this one joins), then from the repository
root:

```sh
docker compose -f connectors/speech/compose.yaml up -d                            # Kokoro
docker compose -f connectors/speech/compose.yaml --profile chatterbox-gpu up -d --build
```

From the api container the engines are then `http://kokoro:8880`, `http://chatterbox:8881` or
`http://chatterbox-gpu:8881`; set the Speech engines Setting to those, the first choice first, as
`http://chatterbox-gpu:8881,http://kokoro:8880`. Each also listens on `127.0.0.1` of the host
(8880, 8881) for a native Swarmify and for seeding voices; they have no sign-in, so never bind
them wider. The models volume is `swarmify_speech-models`, the one V1's compose file made, so an
upgrade keeps the models it downloaded. Updating: `git pull`, then the same command with
`--build`.

Upgrading from V1, where `kokoro` and the `chatterbox` profiles were in Swarmify's own compose
file: run `docker compose up -d --build --remove-orphans` there once, which stops V1's engines
(their ports would clash), then start them from this compose file as above. A `SPEECH_ENDPOINTS`
in Swarmify's `.env` (as `http://chatterbox-gpu:8881,http://kokoro:8880`) is carried into the
Setting on that first start and keeps working, since the engines keep their service names on the
shared network.

## Speech engines

Narration comes from text to speech engines that run as their own services, never inside the core
(ADR 0019). Each speaks OpenAI's `POST /v1/audio/speech` and answers `GET /health`, listing in
`tags` the delivery tags it understands written `[tag]` in the input (Chatterbox Turbo:
`whispering`, `happy`, `angry`, `sarcastic`, `surprised`, `fear`, `crying`, `dramatic`,
`narration`, `advertisement`, `laugh`, `chuckle`, `sigh`, `gasp`, `groan`, `cough`, `sniff`,
`shush`, `clear throat`; Kokoro none). The speech
connector tries them in the order its Speech engines Setting (`SPEECH_ENDPOINTS`) lists.

| Engine | Port | License | Start it |
|---|---|---|---|
| Chatterbox Turbo (Resemble AI), the first choice: natural takes | 8881 | MIT; every take carries Resemble's PerTh watermark, inaudible | natively, below, or a compose profile |
| Kokoro-FastAPI, the fallback: fast on a CPU, plainer voice | 8880 | Apache 2.0 | `docker compose -f connectors/speech/compose.yaml up -d kokoro` |

- `chatterbox/` is its own uv project: PyTorch, `chatterbox-tts`, `faster-whisper` and
  `setuptools<81` (the watermarker needs `pkg_resources`). Natively, from the repository root:
  `cd connectors/speech/chatterbox && uv sync --frozen && PYTORCH_ENABLE_MPS_FALLBACK=1 uv run
  --frozen uvicorn server:app --port 8881`. The first start downloads the models from Hugging
  Face, later starts load them in about 30 seconds, once. On a Mac it runs on the GPU (MPS), so run
  it natively there, not in Docker; elsewhere it runs on the CPU.
- Besides speech it serves `POST /v1/align`: form fields `file` (a take) and `text` (what it says)
  give `{"timestamps": [{"word", "start_time", "end_time"}]}` in seconds, the shape Kokoro-FastAPI's
  `/dev/captioned_speech` returns with its speech. faster-whisper (`small.en`, CPU) hears the words
  and they are matched back to the text, so the text's spelling wins; words it missed or merged
  share the gap between their heard neighbours. Speech takes an optional `seed` for repeatable
  takes; it returns 16-bit WAV only, and Turbo speaks its built-in voice whatever `voice` says,
  unless the request is a multipart form (the same fields) with a `reference` file: then it
  clones that clip (5 to 10 s of speech; a shorter one is a 400), conditioning Turbo once per
  clip and caching it, and `/health` says `"clips": true`. `test_speech_server.py` checks this
  with a fake model.
- On a host without an Apple GPU, Compose runs Chatterbox too: `docker compose -f
  connectors/speech/compose.yaml --profile chatterbox up -d --build`, with the Speech engines
  Setting `http://chatterbox:8881,http://kokoro:8880`. Its models stay in the
  `swarmify_speech-models` volume. It needs about 7 GB of memory on the CPU
  (its peak while loading), more than Docker Desktop's default leaves beside other containers. Both engines bind to `127.0.0.1` only:
  they have no sign-in.
- On a host with an NVIDIA GPU (a driver for CUDA 12.4 or newer and the NVIDIA Container Toolkit),
  use `docker compose -f connectors/speech/compose.yaml --profile chatterbox-gpu up -d --build`
  instead, with the Setting `http://chatterbox-gpu:8881,http://kokoro:8880`. It builds with PyTorch's CUDA
  wheels and `/health` says `"device": "cuda"`; on the CPU Chatterbox runs at about 11x real
  time. Natively on Linux, `uv sync --extra cpu` or `--extra cu124` picks the wheels.
- `uv run pytest connectors/speech --no-cov` includes a smoke test (`test_engines_smoke.py`) that
  speaks a sentence on every engine in `SPEECH_ENDPOINTS` (the environment variable, default the
  two native ports) and aligns it where the engine can, and on an engine advertising
  `whispering` checks a whispered take differs from the plain one and aligns to the same words;
  it skips each engine that is not up.

## The speech connector

`speech` is a stage connector, a plain MCP server (`speech.py`, started as `.venv/bin/python
speech.py` in its installed folder). Everything Swarmify knows about it (name, kind, stage types,
its Setup, a JSON schema of its params, inputs, outputs, the `script` type, its Service) is
`swarmify-connector.toml`; the core never names `speech`.

```yaml
- swarm: script
  goal: Write the voice-over.
  done_when: A script of short scenes.
  output_type: script         # the type speech declares for its input; the check needs it
  produces: [script.md]
- connector: voice
  calls: speech
  input: script.md
  output: timings.json
  params: {seed: 7}           # optional: voice (wins over the script's), engines, seed
- connector: video
  calls: remotion_build
  inputs: [script.md, timings.json]
```

- Its input is the declared type `script` (`[types.script]` in the Manifest, the JSON Schema of
  the block below),
  so the swarm stage writing `script.md` declares `output_type: script` and its judge writes the
  block. A hand-written script, prose included, still works where no swarm stage produces it.
- The script is a fenced JSON block, `{"scenes": [{"id", "lines": [{"text", "pause_after"}]}]}`
  (`pause_after` in seconds, default 0.3; ids are letters, digits, `_` or `-`), else each
  paragraph is a scene and each sentence a line. Frontmatter and `#` headings are skipped. A
  line that is not an object with a `text` string is refused with the scene, the line and the
  shape expected.
- Directions in a line's text, screenplay style, in round or square brackets:
  - `(pause)` and `(beat)` are 0.5 s of silence, `(long pause)` 1.2 s, `(pause 1.5s)` as said.
    At the end of a line they add to its `pause_after`; mid-line they split it in two lines
    with that gap.
  - A delivery direction becomes a tag: `[whispering]` or (whispers), `[laugh]` (laughs),
    `[chuckle]`, `[sigh]`, `[gasp]` and so on as the tag's name or its verb; `[happy]`
    (excited, cheerful, happily), `[dramatic]` (dramatically), `[crying]` (sadly), `[angry]`
    (angrily), `[sarcastic]` (sarcastically), `[surprised]`, `[fear]` (scared, fearfully),
    `[clear throat]`, `[cough]`, `[groan]`, `[sniff]`, `[shush]`, `[narration]`,
    `[advertisement]` (`TAGS` in the connector). An engine hears a tag only when its `/health`
    lists it; for the others it is taken out, so no engine reads a direction aloud.
  - Anything else in brackets is spoken without them and listed in the line's `unrecognized`.
  - Words for alignment and captions never hold a tag or a pause.
- One take per scene. The engines are tried in the order of `SPEECH_ENDPOINTS` (or of
  `params.engines`, which may only reorder or narrow that list, since the script is sent there);
  when one fails, the whole run is redone on the next, so one video never mixes two voices.
  `seed` goes to the engine; the voice is resolved per engine (Voices, below).
- Word times come with the speech when the engine serves Kokoro-FastAPI's
  `/dev/captioned_speech`, else from its `/v1/align`. Either way they are matched back to the
  script, whose spelling wins; words the engine missed or merged share the gap between their
  neighbours.
- It writes the takes to `narration/<scene id>.wav` beside `timings.json`, which holds `engine`,
  `voice` (the voice that spoke: a catalog name, an engine's own voice, or null for the engine's
  default), `chosen` (the voice asked for), `seed`, `warnings` (catalog entries it ignored, and
  a script voice no longer in the catalog) and per take its `file`, `duration` (seconds) and `lines`: each line's `text`,
  `pause_after`, `directions` (its tags, as `["whispering"]`), `unrecognized`, `words` in
  Remotion's `Caption` shape (`text` with the space before it,
  `startMs` and `endMs` from the take's start, `timestampMs` and `confidence` null), and
  `cut_in` and `cut_out` (seconds into the take). A cut between two lines sits in the middle of
  the quietest 10 ms between them; the take keeps 0.05 s before its first word and 0.15 s after
  its last. A line whose cut has no silence (a gap under 10 ms or louder than about -34 dBFS)
  has `joined_next: true`, and the call's `joined` and `message` name it: cut there and the
  word edge shows, so keep the two lines together or voice them again.
- The workflow check wants an input and an output and refuses params outside the Manifest's.
- The run page's speech panel shows the voice that spoke, the one chosen when the engine could
  not speak it, and the warnings.
- Measured on an M3 Max with both engines warm, the spike's four scenes (11 lines): Kokoro 6.6 s
  for 17.7 s of speech, Chatterbox Turbo 33 s for 20.6 s, which flagged "sleep all winter
  long." running into "Good night" as the spike heard it.

## Voices

A voice is chosen per piece of content, and only the speech connector knows what a voice is. A
world's voices are `assets/voices/`, the folder its Manifest declares as its Owned Folder
(`[[owned_folders]]`): each clip there is a voice, and `voices.json`, a list of entries, describes
them:

```json
[
  {"name": "narrator", "description": "Warm and calm, for bedtime facts",
   "tags": ["warm", "calm"], "engine": "chatterbox", "clip": "narrator.wav",
   "consent": "My own recording, 2026-10-29."},
  {"name": "af_heart", "description": "Warm, friendly American woman",
   "tags": ["warm", "friendly"], "engine": "chatterbox", "clip": "af_heart.wav",
   "preset": "af_heart", "consent": "synthetic: Kokoro preset af_heart, Apache 2.0"}
]
```

- `name` is letters, digits, `_` or `-`; `description` and `tags` tell the judge what it fits;
  `engine` is the engine it was made for. `clip` is a reference recording in the same folder
  (Chatterbox clones from 5 to 10 s of clear speech) and `preset` an engine's own voice name
  (Kokoro's `af_heart`); an entry needs one of them, and may have both.
- `consent` is required: a short statement of the right to use the voice (your own recording, or
  a license that allows synthesis). An entry without it, or with a name used twice or a field of
  the wrong kind, is ignored, and a warning names it in the speech stage's `timings.json`, its
  call's message and the run page. An entry whose clip is no longer in the folder (deleted on
  the Assets page) is dropped without a warning.
- Every other `<name>.wav` in the folder, as the Assets page uploads them, is a voice of that
  name (letters, digits, `_` or `-`), described only as "the voice in <name>.wav", for
  Chatterbox, its consent the confirm the upload asked for. One that is not 5 to 10 s of 16-bit
  WAV, or whose name an entry already has, is left out with a warning.
- With a catalog, the connector answers its `script` type for the world with an optional
  top-level `voice` whose enum is the catalog's names, described by each one's description and
  tags (`speech_types`), so the judge of the stage writing the script picks one that fits. It is
  a typed field, so it shows on the script's approval gate and in the script's frontmatter. The
  planner is told the world's voices.
- Selection order: the speech stage's `params.voice` wins (a catalog name, else an engine's own
  voice as before), then the script's `voice`, then the engine's default. A script voice no
  longer in the catalog speaks the default, with a warning.
- Per engine: an engine whose `/health` says `"clips": true` (the Chatterbox service) gets the
  clip as an upload; an engine that lists the preset in `GET /v1/audio/voices` (Kokoro-FastAPI)
  gets the preset. When the engine a run falls back to cannot speak the chosen voice, it speaks
  the catalog entry it can that shares the most tags with it, else its default, and
  `timings.json` records which voice spoke.
- Starter voices without recording anyone: `uv run --project connectors/speech python
  connectors/speech/voices.py <world id> [presets...]` (in Docker, `docker compose exec api sh -c
  'cd /data/packages/speech/1.0.0 && .venv/bin/python voices.py <world id>'`) has Kokoro
  (`KOKORO_URL`, default `http://localhost:8880`; started from this folder's compose file when it
  is not up, or it says so when Docker is unavailable) speak one neutral 9 s
  sentence in each preset, by default eight varied ones (`af_heart` and `am_michael` warm,
  `af_bella` and `am_puck` bright, `am_onyx` and `am_fenrir` deep, `bf_emma` British, `bm_fable`
  a calm narrator), each checked against the engine's voices. It saves each take as
  `<preset>.wav` in the world's `assets/voices/` and writes its entry, clip and preset both, so
  Chatterbox clones the clip and Kokoro speaks the preset itself and a fallback keeps the voice,
  with consent `synthetic: Kokoro preset <name>, Apache 2.0`. Entries of other names stay. It
  reads `SWARMIFY_DATA_DIR` like the app.
- On the world's Assets page the folder is the Voices group (Swarmify's README, Owned Folders on
  the Assets page): every file a card you can play, rename and delete, and "Add to Voices"
  uploads or records a clip, saved as `<name>.wav`, after you confirm it is your voice or you
  have the speaker's permission. Renaming a clip renames the voice; a described one keeps its
  description only if voices.json is edited to match. Swarmify never writes voices.json.

## Tests

`uv run pytest connectors/speech --no-cov` runs the connector against fake engines, installs
this folder through Swarmify's real install path and voices a script over stdio MCP; `chatterbox/`
has its own tests of the service with a fake model.
