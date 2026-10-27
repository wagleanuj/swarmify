# Swarmify

A swarm of Claude Code agents sharing one graphify memory per world, running gated workflows.
Python core (FastAPI, uv workspace, one package per bounded context), React web skin (Vite, pnpm
workspace), one monorepo. Domain docs live in the Obsidian vault, see `CONTEXT.md`.

## Setup

1. Install [mise](https://mise.jdx.dev): `brew install mise`, then activate it in your shell
   (`echo 'eval "$(mise activate zsh)"' >> ~/.zshrc`). mise installs the pinned Python, Node, uv
   and pnpm from `mise.toml`; nothing else is needed globally.
2. `mise install` installs the toolchains.
3. `mise run install` installs Python and web dependencies and Playwright's Chromium.

## Tasks

| Task | What it does |
|---|---|
| `mise run dev` | API with reload on :8000 and Vite on :5173 (proxies `/api`) |
| `mise run lint` | ruff, ruff format, pyright, import-boundary check, tsc |
| `mise run test` | pytest with an 85 percent coverage gate, then Vitest (browser mode) |
| `mise run e2e` | builds the web skin and runs Playwright against the real API |
| `mise run gen-client` | regenerates `apps/web/src/api/schema.d.ts` from the OpenAPI schema |
| `uv run swarmify-mcp` | the swarmify MCP server over stdio (`claude mcp add swarmify -- uv run swarmify-mcp`) |

Data lives in `SWARMIFY_DATA_DIR` (default `~/.swarmify`): `swarmify.db`, the DBOS system database
`dbos.sqlite`, and one folder per world under `worlds/<id>/`. The API streams every world's events at `GET /api/worlds/<id>/events` (SSE,
`Last-Event-ID` resumes); the MCP server writes to the same database, so its actions show up there.

## Workflows

A workflow is YAML saved into a world. Every save is a new immutable version; runs pin the version
they started with. Stages are one of four types, each written as `- <type>: <stage name>`:

```yaml
name: Weekly short
schedule: "0 8 * * 1"      # optional cron
budget_usd: 5              # optional
agents: 4                  # optional
stages:
  - swarm: research        # goal, done_when; optional tools, graphs, output_type, produces
    goal: Find 5 ideas trending this week.
    done_when: 5 ideas ranked
    tools: [web_search, graph]
    produces: [ideas.md]
  - gate: pick             # ask; optional kind: pick | approve | confirm
    ask: Pick one idea.
    kind: pick
  - connector: render      # calls (defaults to the stage name); optional input, output, params
    calls: remotion_render
    input: ideas.md
    output: short.mp4
  - workflow: trends       # runs: a workflow id; optional take: artifacts to copy from the child
    runs: wf_01HX...
    take: [brief.md]
```

Rules checked on every save and by the editor as you type, each reported with its stage and a
rule name: a publish connector must come right after a gate (ADR 0007); workflow stages may not
form a cycle and may nest at most two deep (ADR 0013); every connector must exist in the world and
allow the stage type; a connector's `input` must be produced by an earlier stage.
`POST /api/worlds/<id>/workflows` creates, `POST /api/workflows/<id>/versions` appends,
`POST /api/worlds/<id>/workflows/check` only reports. The MCP server exposes `save_workflow`,
`check_workflow`, `list_workflows` and `get_workflow`.

## Runs

`POST /api/workflows/<id>/runs` (body `{"version": n}` optional, the current version otherwise)
starts a run pinned to that version. Each run is one DBOS workflow (ADR 0014) with the run id as
its workflow id and one DBOS step per side effect: load the run, save each new state, execute a
stage. A crash or restart resumes at the last completed step, so a finished stage never runs again.
Runs wait on a queue partitioned by world, at most two running per world.

Run status is one of queued, running, waiting_gate, paused, done, failed, aborted; stage status one
of pending, running, waiting, done, failed, skipped. The rules live in `swarmify_runs.machine`, a
pure function from (stage kinds, state, event) to (next state, commands); the DBOS workflow only
executes the commands. Every state change appends a `run.updated` event carrying the whole run to
the world's stream, which the runs page and the run timeline render live.

A connector stage calls its connector (see Connectors below), a swarm stage runs its worker (see
Agents below), a gate waits for a person (see Gates below), and a workflow stage runs a child run
(see Child runs below).
`GET /api/worlds/<id>/runs` lists runs newest first; `GET /api/runs/<id>` returns one with its
stages and decisions.

## Child runs

A workflow stage (`- workflow: <name>`, `runs: <workflow id>`, optional `take: [names]`) starts a
child run of that workflow's current version, in whatever world the workflow lives (ADR 0013).
The child is linked both ways: the stage carries `child` (id, world, name) and the child carries
`parent`. The parent's stage stays running and the parent waits on its control topic, as at a
gate, until the child's workflow sends its end. Then each name in `take` is copied from the
child's artifacts (the latest stage that made it wins, sidecars included, frontmatter kept as
provenance) into the parent stage's `artifacts/` folder, with an `artifact.created` event each,
and the parent moves on. A child that fails or is aborted, or a name it never made, fails the
stage with a plain error.

- A child spends its parent's budget: `GET /api/runs/<id>/agents` lists the agents of the run
  and of every run below it, so the run's estimated cost includes its children, and
  `RunControl.family` names the runs a Budget (T15) adds up.
- Aborting a run aborts every run below it; the parent ends aborted, not failed. Aborting only
  a child fails the parent's stage.
- Children wait on their own unpartitioned queue (`child-runs`), so a parent holding one of its
  world's two slots never starves its own child.
- Cycles and nesting deeper than two are refused when a workflow is saved; a run whose pinned
  workflows were edited since also stops a third level with an error.
- A send-back to a workflow stage starts a fresh child; a restart resumes the wait on the same
  one.
- The runs page shows `child of <parent>` on each child, the run timeline links a workflow stage
  to its child and a child to its parent, and the gates inbox labels a child's gate with its
  parent.

## Gates

A gate stage pauses the run until a person answers. `GET /api/gates` is the inbox: every open
gate across all worlds and runs, newest first, each with its `ask`, `kind` and the earlier stages
it may send the run back to. The web skin shows it at `/gates` and renders each kind:

- `pick` takes a `choice` (idea cards arrive with the swarm; until then the pick is typed).
- `approve` approves, or names `send_back_to`, an earlier stage that runs again with the note in
  its context (`notes` on that stage). A run may be sent back twice; the third is refused (409).
- `confirm` takes a plain yes before anything goes out.

`POST /api/runs/<id>/decision` with `{"choice", "note", "send_back_to"}` answers the gate. Every
decision is a markdown artifact with frontmatter in the stage folder,
`worlds/<world>/runs/<run>/<nn>-<stage>/decision.md`, and appears in the run's `decisions`.
`POST /api/runs/<id>/pause`, `/resume` and `/abort` act on the run; a pause lands at once at a gate
and otherwise when the current stage finishes; abort cascades to child runs. The MCP server
exposes `list_gates`, `decide_gate`, `pause_run`, `resume_run`, `abort_run` and `get_run`; it acts
on the API process's runs through DBOS's system database, so it needs the API running.

The Runs context has a `Notifier` port (ADR 0012). `gate.opened` and `run.ended` are appended to
the world's event stream and handed to the notifier; the adapter logs a line until Slack ships
(T16), and tests use a recording fake.

## Connectors

A connector is an MCP server (a command line or an http(s) URL), the credential names it needs,
its kind (tool, stage or publish) and the stage types allowed to use it (swarm, connector). A
publish connector is only ever allowed in connector stages, and the editor makes it follow a gate.
Every world starts with the built-ins: `web_search` and `graph` (swarm tools, no setup),
the stage connector `remotion_render` (see Remotion render below), and the two publish
connectors `youtube` and `upload_post` (see Publishing below).

Credentials live in one file, `<data dir>/.env` (`~/.swarmify/.env`), written with mode 600; the
app refuses to read it when group or others can. Values never enter the database, a run, a
workflow or an API response. A command connector gets exactly its own credentials as its process
environment, merged over the MCP SDK's safe defaults (HOME, PATH, USER, SHELL, TERM, LOGNAME); a
URL connector gets them as request headers of the same name.

- `GET /api/worlds/<id>/connectors` lists built-ins first, each with the credential names still
  `missing` from the file. `PUT /api/connectors/<id>/credentials` (`{"credentials": {NAME:
  value}}`) stores keys for a connector's own names, which is how built-ins get theirs; the
  connectors page has an "Add keys" form per connector.
- `POST /api/worlds/<id>/connectors/test` starts the draft's server once and returns its tools,
  saving nothing. `POST /api/worlds/<id>/connectors` saves it and merges the given credential
  values into the file (an empty value means the file already has it). `DELETE
  /api/connectors/<id>` removes an added one.
- A connector stage calls the tool named like its connector (or `params.tool`) with `params`, its
  `input` and `output` artifact names and `idempotency_key` set to the stage id, through the
  ConnectorTransport port. The call is recorded once per stage id: a finished call is never
  repeated, and a crash mid-job resumes polling instead of calling again.
- A long job answers `{"job_id": ..., "poll": "<tool>"}`; swarmify calls that tool with
  `{"job_id": ...}` every 2 s until it answers `status: done` or `failed`, with optional `progress`
  (0 to 1) and `message`. A wrapper keeps job state outside its process, since every call and
  poll is a fresh MCP session.
- Every change to a call appends a `connector_call.updated` event carrying the whole call, progress
  included; `GET /api/stages/<stage id>/call` returns the raw request and response, which the run
  timeline shows when a connector stage is opened.

A connector stage's call also carries `output_path` (`worlds/<w>/runs/<run>/<stage>/artifacts/<output>`,
folder created; `output` must be a bare file name), `input_path` (the newest file named like
`input` any stage of the run left; the stage fails if there is none) and `frontmatter` (world,
run, stage) for a sidecar. Workflow params may not set these three. When the call is done and the
output file exists, an `artifact.created` event announces it, typed by its sidecar.

## Remotion render

`remotion_render` is our stdio MCP wrapper over `npx remotion render`
(`python -m swarmify_connectors.remotion`). It needs Node on `PATH` and a Remotion project with
its dependencies installed; no credentials.

```yaml
  - connector: render
    calls: remotion_render
    input: script.md          # the script artifact an earlier stage produced
    output: short.mp4
    params:
      project: ~/videos/shorts  # the Remotion project folder
      composition: Short        # the composition ID
      # optional: entry (src/index.ts), codec (h264), frames (0-29), props
```

The composition gets the script as input props, `{"script": <text>, "scriptPath": <path>, ...props}`
(read them with `getInputProps()`). The render runs detached from the MCP session, so a restart of
swarmify resumes polling it; progress comes from the CLI's `Rendered n/N` and `Encoded n/N` lines
and shows on the timeline. On success the video gets a sidecar, `short.mp4.md`, with frontmatter
(type video, author remotion_render, world, run, stage, created, source script, composition,
bytes). On failure the stage fails with the last 40 lines of CLI output, shown under the timeline.
Tests use a fake `npx`; one real render runs when you opt in:
`SWARMIFY_REMOTION_PROJECT=<project> SWARMIFY_REMOTION_COMPOSITION=<id> uv run pytest -k real_render`.

## Publishing

A publish connector posts to the outside world, so a publish stage runs only right after a gate
(ADR 0007, no override). The editor refuses to save a workflow that breaks this, and the runner
refuses to execute such a stage too (it fails the stage), which catches a connector that became
publish after the workflow was saved. Every publish stage declares the same params:

```yaml
  - gate: post it
    kind: confirm
    ask: Post this short?
  - connector: publish
    calls: youtube          # or upload_post
    input: short.mp4        # the newest file of this name any earlier stage left in the run
    params:
      title: Five hooks that work      # required, at most 100 characters
      description: optional
      visibility: public               # public, unlisted or private (default private)
      schedule: "2026-11-03T09:00:00Z" # optional, ISO 8601 with a timezone; needs public
      user: my-profile                 # upload_post only: the Upload-Post profile
      platforms: [youtube, tiktok]     # upload_post only, default [youtube]
```

The gate before a publish stage carries `publish` in `GET /api/gates` (and MCP `list_gates`,
and the `gate.opened` event): connector, title, visibility, schedule, video, platforms and any
`problems` that would stop the post. The gates inbox shows it under the ask and disables
"Confirm and post" while there are problems. The values sent are exactly the previewed ones,
pinned in the workflow version. A publish call is attempted at most once per stage: a call
interrupted by a crash is never repeated, it fails the stage asking you to check the platform.
After the call, `publish.md` (type `publish`, with `links`, `status` and the video or job `id` in
its frontmatter and the links again in its body) lands in the stage's `artifacts/` folder.

- `youtube` is our wrapper over the YouTube Data API (`swarmify_connectors.youtube`, stdio MCP).
  It uploads with `videos.insert`; with a schedule the video goes up private with `publishAt` and
  YouTube makes it public at that time. Setup, once: create a Google Cloud project, enable the
  YouTube Data API v3, create an OAuth client of type Desktop app, then run
  `YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... uv run python -m swarmify_connectors.youtube authorize`,
  allow the upload scope in the browser, and store the three keys (`YOUTUBE_CLIENT_ID`,
  `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`) with "Add keys" on the connectors page. The
  default quota allows 100 uploads a day.
- `upload_post` is Upload-Post's official MCP (`npx -y @upload-post/mcp@0.11.4`, needs Node),
  adopted as is (ADR 0015) with `UPLOAD_POST_API_KEY` from app.upload-post.com. Swarmify calls its
  `upload_video` tool with the stage id as Upload-Post's idempotency key. Posting now records
  each platform's link (a platform that failed is listed in the artifact; when none succeeded the
  stage fails). A scheduled post records the Upload-Post job id; its links appear only once it
  goes out, which the view-count connector will write back later.

Tests never reach either service: the fake ConnectorTransport stands in for both MCP servers,
and the YouTube wrapper's own tests answer its HTTP requests with httpx's MockTransport.

## Whiteboard

Each swarm stage has a whiteboard of tasks after the beads model (ADR 0017): a task has a status
(open, claimed, done), an owner, dependencies on other tasks of the stage, a heartbeat, notes and
who added it. A task is claimable once every task it depends on is done. A claim is one
`BEGIN IMMEDIATE` transaction updating the row only while it has no owner, so of any number of
concurrent claims exactly one wins and the rest get a 409. The owner heartbeats, optionally with
partial notes; a claim whose last heartbeat is older than two minutes returns to open with its
notes kept and a note saying why (the API sweeps every 15 seconds). Every change appends a
`task.updated` event carrying the whole task, which the run page's whiteboard renders live.

| Action | HTTP | MCP |
|---|---|---|
| Add a task (`title`, optional `body`, `depends_on`, `added_by`) | `POST /api/runs/<run>/stages/<stage>/tasks` | `add_task` |
| List a stage's tasks | `GET /api/runs/<run>/stages/<stage>/tasks` | `list_tasks` |
| Claim, heartbeat, finish (`agent`, optional `note`) | `POST /api/tasks/<id>/claim`, `/heartbeat`, `/finish` | in-process agent tools (T07) |

Tasks can be added to a swarm stage that has not ended; the run page's whiteboard has an add form
for this. The HTTP claim, heartbeat and finish stay alongside the agents' in-process tools, for
operators and for agents outside swarmify (a Claude Code session driving a stage by hand).

## Agents

A swarm stage runs one worker (`worker-1`, Gilfoyle in the skin) through the AgentRuntime port.
The adapter is the Claude Agent SDK, whose wheel bundles the `claude` CLI; sessions run on your
Claude subscription (ADR 0010), so for a headless host run `claude setup-token` once and export
`CLAUDE_CODE_OAUTH_TOKEN`. The planner seeds an empty whiteboard, the worker works it, and the
judge decides when the stage is done (see Planner and judge below).

- In-process tools (`mcp__swarmify__<name>`): `query_graph`, `list_tasks`, `claim_task` (the named
  task or the next ready one), `heartbeat`, `finish_task`, `add_task`, `write_note`,
  `write_artifact`. Notes land in `worlds/<w>/runs/<run>/<stage>/notes/`, artifacts in
  `.../artifacts/`, each as markdown with frontmatter (a non-markdown artifact gets a `.md`
  sidecar), and each write appends an `artifact.created` event. Graph tools (`query_graph`,
  `get_node`, `get_neighbors`, `god_nodes`, `shortest_path`, `graph_stats`) are described under
  Graph below.
- Allowlist: the tools above, `Skill`, `Read` (inside the world folder only), plus per stage
  `web_search` (WebSearch, WebFetch) and any connector named in `tools` (`mcp__<name>__*`, its
  server and credentials passed to the session).
- Hooks: a PreToolUse hook writes every tool call to the stage's live log, denies anything outside
  the allowlist and denies a claim until the agent has queried the graph. Each result's estimated
  cost (the SDK's estimate, not billing) is kept on the agent; the run's cost is the sum.
- Skills: the world folder is passed as a local plugin with host settings off
  (`setting_sources=[]`, ADR 0016), so `worlds/<w>/skills/<name>/SKILL.md` loads as
  `<world id>:<name>` and nothing from `~/.claude` does. The init message's skill list is stored on
  the agent and logged; plugin errors are logged as errors.
- Sessions: the session id is stored on the agent the moment the session starts. A session that
  dies is replaced once by a session resuming that id; when both die the stage fails. A retried
  stage whose worker finished does not run it again.

`GET /api/runs/<id>/agents` lists a run's agents (status, session, skills, cost) and `GET
/api/runs/<run>/stages/<stage>/log` a stage's live log; `agent.updated` and `agent.logged` events
stream both, and the run page shows them under the timeline.

Tests drive the real SDK client with `swarmify_swarm.fake.Script`, a scripted `claude` CLI behind
the SDK's Transport seam: it answers the initialize handshake, fires the registered hook callbacks
and calls in-process tools over the control protocol, so hooks and tools run for real without a
model. The e2e server (`tests/e2e/scripted_app.py`) runs the same script.

## Graph

Each world's graph is `worlds/<w>/graphify-out/graph.json`, built by graphify (`graphifyy`, a
dependency of `packages/memory`) from the world's files.

- GraphWriter (Anton in the skin, ADR 0003) is the only writer. It polls the world folders every
  2 s, waits until a world has been quiet for 30 s after its last change, then runs
  `python -m graphify extract <world> --backend claude-cli --exclude graphify-out/`. One thread
  does every rebuild in turn: serialized per world, queued across worlds. Each rebuild appends
  `graph.rebuilt` (`nodes`, `edges`, `nodes_added`, `edges_added`, `seconds`) or `graph.failed`
  (`error`) to the world's stream. It runs inside the API process; with
  `SWARMIFY_GRAPH_WRITER=external` the API leaves it to a `swarmify-graph-writer` process instead
  (the compose file does this). graphify's Claude CLI backend needs `claude` on PATH and logged in.
- GraphIndex port: `McpGraphIndex` runs one `python -m graphify.serve` stdio process for every
  world, picking the graph per call with `project_path`; `FakeGraphIndex` answers from the graph
  file for tests.
- Agents' graph tools take an optional `world`: their own world by default, or a world the stage
  lists under `graphs` (by name or id). Any other world or path is refused, and answers are
  labelled `[world <name>]` (ADR 0009).
- `GET /api/worlds/<id>/graph` returns size, god nodes, the last rebuild and the last failure;
  `POST /api/worlds/<id>/graph/query` (`{"question": ...}`) asks the graph. The page is
  `/worlds/<id>/graph` (Middle-out) and reloads on `graph.rebuilt` and `graph.failed`.
- Real graphify runs only in the opt-in test, which spends Claude subscription tokens:
  `SWARMIFY_GRAPHIFY=1 uv run pytest packages/memory/tests/test_graphify_live.py --no-cov`.

`docker compose up --build` builds the api image (web skin baked in, served at `/`) and the
graph writer.

## Planner and judge

A swarm stage runs three kinds of agent session on the same runtime: `planner` (Richard in the
skin), `worker-1` (Gilfoyle) and `judge` (Erlich). Planner and judge are short sessions that may
only read (`query_graph`, `list_tasks`, `Skill`, `Read`) and must answer structured JSON
(`--json-schema`, the SDK's `output_format`); an answer that does not parse counts as a failed
session, replaced once like any other.

1. **Plan.** On an empty whiteboard the planner answers `{"tasks": [{"title", "body", "after"}]}`
   (`after`: indexes of earlier tasks it depends on); the tasks land in one transaction,
   `added_by: planner`.
2. **Work.** The worker runs while any task is not done. A worker that ends with tasks left fails
   the stage, so the judge only sees a worked-out board.
3. **Judge.** The judge gets the goal, `done_when`, the whiteboard and every note of the stage with
   numbered paragraphs, and answers `{"passed", "reasons", "gaps", "items"}`. Each verdict is a row
   (`verdict.created` event) and an artifact `artifacts/verdict-<loop>.md`. A fail adds its gaps as
   open tasks (`added_by: judge`; a fail without gaps gets one per reason) and the stage loops.
   Three gap loops are allowed: the fourth failed verdict fails the stage.
4. **Project.** On a pass with an `output_type` (`idea`, or `list[idea]` for several), each item
   becomes `artifacts/<type>-<slug>.md` with frontmatter `type`, `author`, `run`, `stage`, `title`,
   `rank`, its fields (an idea's `hook` and `why`) and `sources: ["<note path>#p<n>"]`, keeping only
   references to a real paragraph of one of the stage's notes, quoted under Sources in the body
   (ADR 0011). A single type keeps the best ranked item.

A retried stage resumes from what is stored: a seeded board is not planned again, a worked board is
not worked again, a passing verdict is projected again from its stored items without a new judge
session. `GET /api/runs/<run>/stages/<stage>/verdicts` lists the verdicts and
`GET /api/runs/<run>/stages/<stage>/artifacts` the stage's artifact files with their frontmatter;
the run page shows both under the timeline, live.

Scripts can answer for a session with `Answer(data)` (the structured output) and refer to the task
just claimed with `LAST`; `Script(..., cycle=True)` repeats the step lists per run.

## Notifications

Each world has at most one Channel: a Slack incoming webhook, set on the world's settings page
(`/worlds/<id>/settings`). A Notification goes out when a gate opens (a budget pause is a gate of
kind `budget`), on `budget.warning` at 80 percent, and when a run is done, failed or aborted. Each
message carries a deep link: gates to `/gates`, everything else to the run timeline.

- `SWARMIFY_BASE_URL` (default `http://localhost:8000`) is where the links point; set it to the
  address you open the app at, such as `http://mac-mini.local:8000`.
- `GET` and `PUT /api/worlds/<id>/notifications` (`{"slack_webhook": "https://hooks.slack.com/..."}`,
  blank removes it) read and set the webhook. The URL is a credential: it is returned only as
  scheme, host and its last four characters. `POST /api/worlds/<id>/notifications/test` posts a
  test message (409 with no webhook, 502 when Slack refuses).
- `packages/notifications` holds `Notifications`, the Slack adapter of the Runs Notifier port, and
  `FakeSlack`, a real HTTP server on localhost that tests point the webhook at. A Slack outage is
  logged and never fails a run; the event is still on the world's stream.
- To get a webhook: create a Slack app at api.slack.com/apps, turn on Incoming Webhooks, add one
  to your channel and paste its URL into the settings page.

## Library, pins, forks and promotes

The Library is every artifact of a world across its runs, read from the files (ADR 0001): the
typed artifacts the judge projected, the files workers and connectors left, every verdict and
every gate decision. `GET /api/worlds/<id>/library` lists them newest first, each with its type,
title, frontmatter fields, run, workflow version and stage; the page is `/worlds/<id>/library`,
filterable by type, workflow, stage, run and date, and it reloads on `artifact.created`.

- **Pin.** `POST /api/worlds/<id>/pins` (`{"path": "<path in the world folder>", "pinned": true}`)
  marks an artifact as required reading; `pinned: false` clears it. The flag is `pinned: true` in
  the markdown's frontmatter (a binary's in its sidecar), so it survives any database, and every
  change appends `artifact.pinned`. The planner and the worker of every later swarm stage of the
  world get the pinned paths in their prompts under "Pinned references, required reading"; the
  judge does not, it judges the stage's own notes.
- **Fork.** `POST /api/runs/<id>/fork` (`{"stage": n}`) starts a new run of the same workflow
  version that copies the source up to stage `n` and runs on from there: the earlier stages'
  rows keep their status and times, their folders (notes, artifacts, decisions) are copied under
  the fork's own stage ids, and the fork never runs them again. Every stage before `n` must have
  finished (409 otherwise). The fork carries `forked_from` and `fork_stage_index`; the source
  lists it under `forks`. The run timeline offers "Fork from here" on every finished stage, so a
  pick gate can be answered differently without redoing the research.
- **Promote.** `POST /api/workflows/<id>/promote` (`{"artifact": "<path>", "stage": n, "version":
  optional}`) starts a run at stage `n` with a Library artifact as its input: stages before `n`
  are inserted `skipped`, the file (with its sidecar, frontmatter kept as provenance) lands in the
  last skipped stage's `artifacts/` folder where later stages' `input` finds it, and the run
  carries `promoted_from`. `n` must skip at least one stage and start at or before the last (422). The
  Library's "Promote..." picks the workflow and the stage.

## Layout

- `packages/kernel` shared kernel: ids, the event envelope and its append-only log, SQLite
  access and the migration runner. The only package other packages may import.
- Migrations are numbered SQL files, `NNNN_name.sql`, applied in order and recorded in
  `PRAGMA user_version`. Numbers are global: each package ships its own `migrations/` folder
  (kernel 0001, worlds 0002, workflows 0003, runs 0004, connectors 0005, swarm 0006 and 0007, runs 0008,
  0010 and 0013, swarm 0009, notifications 0016, ...) and the composition root passes them all to `migrate` once.
  Adapters never migrate.
- `packages/<context>` one package per bounded context (worlds, workflows, runs, swarm, memory,
  connectors, learning, notifications). `packages/swarm` also declares AgentRuntime, with the
  Agent SDK adapter `SdkRuntime`; `packages/memory` writes notes and artifacts as files and declares GraphIndex, with
  `McpGraphIndex` and `FakeGraphIndex`, plus the GraphWriter. `tools/check_imports.py` fails CI if a context imports
  anything but the kernel and itself. Each context declares its ports in `ports.py` (ADR 0012);
  `packages/worlds`, `packages/workflows`, `packages/runs`, `packages/connectors`, `packages/swarm` and
  `packages/notifications` each have a Storage port with a SQLite adapter.
  Workflows learns which connectors a world allows through a function the composition root passes
  in, backed by the connector registry. `packages/connectors` also declares ConnectorTransport, with
  the MCP adapter and `FakeTransport` for tests.
- `apps/api` FastAPI composition root; serves `apps/web/dist` at `/` when it exists. Also home of
  the `swarmify-mcp` server, which exposes the same actions as MCP tools.
- `apps/web` React skin with a typed client generated by openapi-typescript.
- `tests/e2e` Playwright for Python against the real API.
