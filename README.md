# Swarmify

A swarm of Claude Code agents sharing one graphify memory per world, running gated workflows.
Use it for research briefs with cited sources, drafts you approve or send back, scheduled
monitors that return a typed list of what changed, or a weekly short video; what a workflow makes
comes from its stages and the connectors a world has, not from Swarmify itself.

Python core (FastAPI, uv workspace, one package per bounded context), React web skin (Vite, pnpm
workspace), one monorepo. Domain docs live in the Obsidian vault, see `CONTEXT.md`.

## Setup

1. Install [mise](https://mise.jdx.dev): `brew install mise`, then activate it in your shell
   (`echo 'eval "$(mise activate zsh)"' >> ~/.zshrc`). mise installs the pinned Python, Node, uv
   and pnpm from `mise.toml`; nothing else is needed globally.
2. `mise install` installs the toolchains.
3. `mise run install` installs Python and web dependencies and Playwright's Chromium.

Run pnpm through mise (`mise run ...` or `mise x -- pnpm ...`). A bare `pnpm` may resolve to
corepack, which cannot download the pinned pnpm and exits with an error that is easy to miss
behind a pipe, so the web tests silently never run.

## Tasks

| Task | What it does |
|---|---|
| `mise run dev` | API with reload on :8000 and Vite on :5173 (proxies `/api`) |
| `mise run lint` | ruff, ruff format, pyright, import-boundary check, tsc |
| `mise run test` | pytest with an 85 percent coverage gate, then Vitest (browser mode) |
| `mise run e2e` | builds the web skin and runs Playwright against the real API |
| `mise run gen-client` | regenerates `apps/web/src/api/schema.d.ts` from the OpenAPI schema |
| `uv run swarmify-eval` | the baseline against swarm eval, against the running API with the real model (see Eval below) |
| `uv run swarmify-mcp` | the swarmify MCP server over stdio (`claude mcp add swarmify -- uv run swarmify-mcp`); over HTTP it is `/mcp` with an API token (see Sign-in) |

Data lives in `SWARMIFY_DATA_DIR` (default `~/.swarmify`): `swarmify.db`, the DBOS system database
`dbos.sqlite`, and one folder per world under `worlds/<id>/`. The API streams every world's events at `GET /api/worlds/<id>/events` (SSE,
`Last-Event-ID` resumes, and without it `?since=now` starts at the current head instead of
replaying history; every stream opens with an id-only frame naming the seq it starts after, so a
reconnect before the first event still resumes; `GET /api/events` takes the same for every world); the MCP server writes to the same database, so its actions show up there.

## Workflows

A workflow is YAML saved into a world. Every save is a new immutable version; runs pin the version
they started with. Stages are one of four types, each written as `- <type>: <stage name>`:

```yaml
name: Weekly brief
schedule: "0 8 * * 1"      # optional cron
budget_usd: 5              # optional
agents: 4                  # optional
stages:
  - swarm: research        # goal, done_when; optional tools, graphs, output_type, produces, model
    goal: Find what changed in heat pumps this week.
    done_when: 5 findings ranked, each with a source link
    tools: [web_search, graph]
    produces: [findings.md]
  - gate: pick             # ask; optional kind: pick | approve | confirm
    ask: Pick the finding worth a brief.
    kind: pick
  - connector: render      # calls (defaults to the stage name); optional input (or inputs: [a, b]), output, params
    calls: remotion_render # any connector the world has; this one renders a video
    input: findings.md
    output: short.mp4
    params: {project: ~/videos/shorts, composition: Short}
  - workflow: trends       # runs: a workflow id; optional take: artifacts to copy from the child
    runs: wf_01HX...
    take: [brief.md]
```

Rules checked on every save and by the editor as you type, each reported with its stage and a
rule name: a publish connector must come right after a gate (ADR 0007); workflow stages may not
form a cycle and may nest at most two deep (ADR 0013); every connector must exist in the world and
allow the stage type; a connector's `input`, and each of its `inputs`, must be produced by an earlier stage (a stage names
`input` or `inputs`, not both); a swarm stage may `produce` only text files (`video.mp4` comes from
a connector stage, `remotion_build` from a script, since swarm agents write text); and a connector stage
must pass its connector's own check (`connector_params`): a publish stage needs `title` or
`metadata`, an `input` and valid params (the problems its gate would show), a `remotion_render`
stage needs a `project` folder that exists and a `composition`, a `remotion_build` stage an
`input` (or `inputs`) and, when it names one, a `base` template that exists. A typed input (see
Typed contracts) read from a swarm stage's `produces` needs that stage to declare the same
`output_type` (`output_type`, naming both stages and the type), and two connectors declaring one
type name with different schemas fail the stage that names it (`type_conflict`). So a broken stage is caught when
the workflow is saved, not after the stages before it spent the budget. The editor's planner is
told each built-in connector's params and every typed input of the world's connectors, so it
drafts the `output_type` they need.
`POST /api/worlds/<id>/workflows` creates, `POST /api/workflows/<id>/versions` appends,
`POST /api/worlds/<id>/workflows/check` only reports (violations, the parsed model and, with a
`schedule`, `next_run_at`). The MCP server exposes `save_workflow`, `check_workflow`,
`list_workflows` and `get_workflow`.

### Workflow editor

The editor at `/worlds/<id>/workflows/new` (and `/<workflow id>`) has three views of one text:
**Planner** (the planner chat), **Stages** (a visual list: workflow fields, one card per stage
with its fields, move up and down, remove, add by type) and **YAML**. Every edit in any view
rewrites the YAML, which the other views project, and the check runs on each change. The
aside shows the stage rail, the schedule with its next run, the versions, and "Copy to another
world".

- **Starter workflows.** A new workflow opens on the first of four starters, the others one
  click away above the tabs: a research brief (one swarm writes a cited `brief.md`), a draft with
  approval (outline, draft, an approve gate), a scheduled monitor (every Monday, a `list[finding]`)
  and the weekly short (ranked ideas, then a pick). Each uses only stage types and tools every
  world has and passes the check as it stands; `<your topic>` and `<your question>` are yours to
  fill. Picking another replaces an untouched draft and asks first when you have edited it.
  `GET /api/workflows/starters` lists them (`swarmify_workflows.starters`).
- **Planner chat.** `POST /api/worlds/<id>/planner` with `{"message", "yaml", "workflow_id",
  "session_id"}` runs one turn of a Planner session on the AgentRuntime whose tools are the
  swarmify MCP server's own `check_workflow`, `save_workflow`, `get_workflow`,
  `list_workflows` and `start_run` (only when asked to run a saved workflow), called in-process. It answers `{"reply", "yaml", "session_id", "saved"}`:
  `yaml` is the whole draft after the planner's changes (the editor adopts it, unsaved),
  `session_id` continues the conversation next turn, and `saved` is the version the planner
  saved when asked to. A session that fails or answers off-schema is a 502; send again.
- **Schedule.** `schedule` is a cron expression in UTC (five fields, or six with seconds
  first), checked on save with the same parser DBOS uses. Saving a version registers (or
  removes) a DBOS cron for the workflow; each fire starts an ordinary run of the current
  version, which appears on the runs page like any other. At launch the API makes DBOS's
  schedules match the workflows table, so a save from the MCP process while the API was down
  is picked up then. Deleting a world removes its schedules.
- **Save as workflow.** `POST /api/runs/<id>/workflow` (`{"name"}` optional) turns a done run's
  stages, exactly as the run recorded them, into a new workflow in its world; the run timeline
  has the button. An ad hoc run (T20) becomes repeatable the same way. Tasks the operator added
  to a swarm stage by hand (`added_by` `operator`) are kept: the saved stage's goal ends with
  "Also do these tasks:" and one line per task, so the next run's planner seeds them again.
- **Copy.** `POST /api/workflows/<id>/copy` with `{"world_id"}` saves the current version as a
  new workflow in that world, refused with the violations when the world lacks a connector.

## Runs

`POST /api/workflows/<id>/runs` (body `{"version": n}` optional, the current version otherwise)
starts a run pinned to that version; optional `budget_usd` (see Budget and baseline) and `agents`
(1 to 8 workers per swarm stage) override the workflow's. A run's worker count is `agents` in the
start body, else the workflow's `agents`, else the world's agent count; each swarm stage carries it
in its config, so forks and restarts keep it, and saving the run as a workflow writes it back as
`agents`. The world overview starts a workflow's run with one click, taking the budget and worker
fields above the list when filled. `POST /api/worlds/<id>/runs` with `{"goal": "..."}` (optional
`done_when`, `budget_usd`, `baseline`) starts an Ad Hoc Run: one swarm stage on the typed goal
with every connector of the world that swarm stages may use as its tools, no workflow pinned
(`workflow_id` and `workflow_version` are null, the runs page says `ad hoc`), its notes feeding
the world's graph like any run's. The world overview has the form, its button "Run this goal"
with an optional budget.

Every start of a run of a workflow version (start, promote, fork, a scheduled tick, a child run,
MCP `start_run` and `fork_run`) first runs today's workflow check on that version, since rules
and connectors change after a version is saved. A version with violations is refused: 409 over
HTTP with `detail` and `violations`, the reason in the skin, `{"violations": [...]}` from MCP; a
scheduled tick logs it and starts nothing, and a child run's workflow stage fails with it.
`GET /api/worlds/<id>/workflows` lists each workflow's current `violations`, and the world
overview marks a workflow that fails the check.

Each run is one DBOS workflow (ADR 0014) with the run id as
its workflow id and one DBOS step per side effect: load the run, save each new state, execute a
stage. A crash or restart resumes at the last completed step, so a finished stage never runs again.
Runs wait on their world's own DBOS queue, at most the world's run cap running at once (two by
default, a world setting that takes effect live).

Run status is one of queued, running, waiting_gate, paused, done, failed, aborted; stage status one
of pending, running, waiting, done, failed, skipped. The rules live in `swarmify_runs.machine`, a
pure function from (stage kinds, state, event) to (next state, commands); the DBOS workflow only
executes the commands. Every state change appends a `run.updated` event carrying the whole run to
the world's stream, which the runs page and the run timeline render live.

A connector stage calls its connector (see Connectors below), a swarm stage runs its worker (see
Agents below), a gate waits for a person (see Gates below), and a workflow stage runs a child run
(see Child runs below).
`GET /api/worlds/<id>/runs` lists runs newest first; `GET /api/runs/<id>` returns one with its
stages and decisions. Each stage names the connector it `calls` (connector stages) and what it
`ask`s (gates), so a page can tell a speech stage from a publish stage and show a gate's question
after it was answered. The runs page filters by workflow (or ad hoc) and status, and shows each
run's duration.

### The run page

The run page reads the run, its events and its artifacts only (ADR 0002), top to bottom:

- **Status line**, one sentence: `<stage> is working · step i of n · m so far`, `Waiting for you:
  <the gate's ask>` with `Answer now` (the gate opens on the page), `Done in <duration>.` with where
  it was posted and `Open the post`, `Stopped at <stage>: <error>` with `Fork from here`, or
  `Paused at` / `Aborted at`. A failed or aborted run says its finished stages keep their artifacts,
  and `Nothing was posted.` when a publish stage did not run.
- **Result**: the run's latest primary output in place: a video or audio player, an image, a
  document's first lines with `Open`, or a list's item cards. At a review gate it is the thing under
  review.
- **Spend**: `$x of $y budget` with its bar; `Compare with one agent` opens the baseline note.
- **Stage rail**: a step per stage with its type, state and duration. Clicking one selects it and
  keeps it in the URL as `?stage=<id>`; by default the current stage, else the one that made the
  result, else the last.
- **Stage panel** for the selected step: a swarm's agents (what each does now, in words), whiteboard,
  verdict and artifacts; a gate's ask and your decision; a speech stage's takes per scene with their
  length, engine and any join with no silence; a build's session log in words, its render progress
  (`Rendered 412 of 870 frames`), the video and its project folder; a render's progress and video; a
  publish stage's title, place, visibility, time, link and views; the retro's proposal and how it
  went. Every connector stage keeps the raw call under `Technical details`.
- **Live log** of the selected stage in words; a JSON answer collapses to one line (`Planned 5
  tasks`) that opens.

## Budget and baseline

A run's Budget (Russ in the Pied Piper skin) is its spend ceiling in US dollars. One rule
(`Runner.budget_for`) sets it for every way a run starts (start, ad hoc, fork, promote, schedule):
`budget_usd` in the start body (`POST /api/workflows/<id>/runs` or `/api/worlds/<id>/runs`), else
the workflow's `budget_usd` (a fork keeps its source run's Budget), else 5 USD
(`budget.DEFAULT_USD`), then capped by the world's Monthly Budget (below). A child run spends its
parent's. Spend is the Agent SDK's estimate, not your bill.

- Cost hook: every model step of an agent session (one per tool call) adds an estimate priced
  from the step's token usage at the price of the model it ran on (`swarmify_swarm.sdk.PRICES`:
  Claude Opus 5.5, Sonnet 5.5 and Haiku 4.5; any other model is priced at the most expensive of
  them, so the Budget errs high), and the session's result trues the sum up to the SDK's own total.
  After each estimate the hooks ask the Budget, which refreshes the run's `spent_usd` (the run and
  every child run below it, ADR 0013) and streams it as `run.updated`.
- At 80 percent a `budget.warning` event goes to the world's stream and the Notifier, once per
  budget value. At 100 percent the agents stop: the session ends where it is (its id kept), further
  tool calls are denied, and the stage waits at a budget gate (`gate.opened` with kind `budget`,
  run status `waiting_gate`, `over_budget` true). An agent (planner, worker or judge) whose session
  would start over budget does not start.
- The budget gate takes money or an abort, never a decision. `POST /api/runs/<id>/budget` with
  `{"budget_usd": total}` sets the new total (not an increment, so a double click adds nothing
  twice; it must be above what was spent); the stopped stage runs again and its agent resumes its
  session. `POST /api/runs/<id>/abort` ends the run. The MCP server exposes `set_budget`.
- Monthly Budget: a world setting (`monthly_budget_usd`, none by default). Every run that starts
  gets its Budget cut down to what is left of the calendar month (the sum of the world's top
  runs' `spent_usd` this month, `GET /api/worlds/<id>/spend`); once that is spent, starting a run
  is refused with 409 until the budget is raised or the month turns, and a scheduled tick starts
  nothing.
- Baseline (Hooli in the Pied Piper skin): `{"baseline": true}` in the start body runs every swarm stage with
  one worker and no graph: no planner (the goal is its one task), no judge (so no typed
  projection), no graph tools and no graph-first rule. `GET /api/runs/<id>/baseline`
  returns the newest finished baseline of the same workflow, and the run page shows its cost and
  duration beside the run's own.

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
  its context (`notes` on that stage). Each gate counts its own send-backs: it may send the run
  back `max_send_backs` times (2 when the workflow leaves it out), and the next is refused (409).
  The inbox shows how many revisions the gate has left. A stage that runs again with work
  already done revises it: a swarm stage gets one rework task saying so, with the note and its
  own previous artifacts as required reading (a `remotion_build` stage, below, its last build).
  Only the note of the send-back that caused this visit reaches a stage: a later stage that
  reruns because the send-back named an earlier one gets no note, not an older one of its own.
- `confirm` takes a plain yes before anything goes out.

`POST /api/runs/<id>/decision` with `{"choice", "note", "send_back_to"}` answers the gate. Every
decision is a markdown artifact with frontmatter in the stage folder,
`worlds/<world>/runs/<run>/stage_<id>/decision.md`, and appears in the run's `decisions`.
The newest decision of each gate (its choice, the artifact it names and the note) is required
reading in the planner's and every worker's prompt of each later swarm stage; a gate whose newest
decision is a send-back is left out until it decides again.
`POST /api/runs/<id>/pause`, `/resume` and `/abort` act on the run; a pause lands at once at a gate.
Mid swarm or retro stage, pause and abort stop every agent at its next step, like a spent Budget:
a paused stage goes back to pending and runs again on resume, its agents resuming their sessions;
an aborted one fails. A connector stage, or a stage waiting on a child run, is not stopped midway:
the pause lands when it finishes. Abort cascades to child runs. The MCP server
exposes `list_gates`, `decide_gate`, `pause_run`, `resume_run`, `abort_run` and `get_run`; it acts
on the API process's runs through DBOS's system database, so it needs the API running. It also
has `start_run` (the start body's fields), `fork_run` (`run_id`, `stage`) and `list_artifacts`
(`run_id`, optional `before`, as `GET /api/runs/<id>/artifacts`). The MCP process hosts no DBOS,
so a run it starts or forks is recorded queued and the API enqueues it on its next sweep (every
15 seconds, `Runner.adopt`).

The Runs context has a `Notifier` port (ADR 0012). `gate.opened` and `run.ended` are appended to
the world's event stream and handed to the notifier; the adapter logs a line until Slack ships
(T16), and tests use a recording fake.

## Connectors

A connector is an MCP server (a command line or an http(s) URL), the credential names it needs,
its kind (tool, stage or publish) and the stage types allowed to use it (swarm, connector). A
publish connector is only ever allowed in connector stages, and the editor makes it follow a gate.
Every world starts with the built-ins: the stage connectors `remotion_render` and `remotion_build` (see Remotion render and Remotion
build below) and the two publish
connectors `youtube` and `upload_post` (see Publishing below), and with the default packages
(see Connector Packages), today `speech` (see Speech).

`web_search` and `graph` are not connectors but Agent Tools: the core gives them to swarm stages,
with no package, version or credentials. A swarm stage lists them in `tools` like a tool
connector (`tools: [web_search, graph]`); the Connectors page does not show them and no connector
may take their names.

The built-ins' credentials live in one file, `<data dir>/.env` (`~/.swarmify/.env`), written with
mode 600 (a package's live in its Setup, below); the
app refuses to read it when group or others can. Values never enter the database, a run, a
workflow or an API response. A command connector gets exactly its own credentials as its process
environment, merged over the MCP SDK's safe defaults (HOME, PATH, USER, SHELL, TERM, LOGNAME); a
URL connector gets them as request headers of the same name.

The file is shared by every world: keys saved on one world's connectors page are the keys every
world uses, so two worlds cannot post to different YouTube channels yet, and the page says so.

- `GET /api/worlds/<id>/connectors` lists built-ins first, each with the credential names still
  `missing` from the file. `PUT /api/connectors/<id>/credentials` (`{"credentials": {NAME:
  value}}`) stores keys for a connector's own names, which is how built-ins get theirs; the
  connectors page has an "Add keys" form per connector. Once all of `youtube`'s or
  `upload_post`'s keys are saved, the same request checks them live and answers `check`
  (`{"ok": ..., "message": ...}`): `youtube` refreshes its token and reads `channels.list`
  (mine, 1 quota unit) through its `channel` tool, naming the channel; `upload_post` calls
  Upload-Post's `get_account_info`, naming the account. A failing check still saves the keys and
  the page warns, so a bad refresh token shows then rather than at publish. `check` is null for
  any other connector.
- `GET /api/connectors/<id>/health` calls the connector's health tool, the one its `health`
  field names, and answers `[{"name", "ok", "message"}]`, one per thing it depends on. It is `[]`
  for a connector without one, and 502 when the tool cannot be called. A package's is
  `GET /api/worlds/<id>/packages/<name>/health`: first a GET of each Service it declares
  (`[[services]]`, at each URL its Setting holds, a comma-separated list being several, plus the
  service's `health` path), then its health tool's checks. The connectors page shows them as up
  or down when it opens.
- Typed contracts (issue 133): a connector may declare named input types, each a JSON Schema
  (`types`, as `{"recipe": {...}}`), and which of a stage's inputs takes which (`input_types`, by
  position over `input` or `inputs`, null for an untyped one). Built-ins and packages declare
  them in their Manifest, as `speech` declares `script`; an added connector gives them in its
  `POST /api/worlds/<id>/connectors` body, checked to be lower_snake_case names of valid
  schemas. A swarm stage promises a type with `output_type: <name>`: its judge then answers each
  item's `fields` in the type's schema (structured output), an answer that does not fit fails
  that judge session, and the artifact carries the value as a fenced JSON block before the
  readable body; its top-level text fields (a script's `description`, say) also go in the
  frontmatter beside the title, where a publish stage's `metadata` reads them. A single typed output is written as the stage's produced markdown file (the one
  named after the type, else the first), so the connector stage reading that file gets it; a
  `list[<name>]` writes `<name>-<slug>.md` files. A type name no connector declares (`idea`)
  keeps the generic projection. The check refuses a workflow, and a run start re-checks it
  (#102), when a typed input comes from a swarm stage without that `output_type`. Agents still
  work freely in notes; only the judge's projection is typed (ADR 0011).
- World-dependent types (issue 140): a connector may name a tool (`world_types` in its manifest
  or its `POST` body) that answers its types for one world. The registry calls it with `world`
  (the id) and `assets_path` (the world's assets folder) at the workflow check (only for a type
  some swarm stage promises), at the run-start re-check and when the judge projects, and expects
  `{"types": {name: schema}, "note": "..."}`. Only a type the connector declares may change, and
  only to a valid JSON Schema; a type it does not answer, or every type when the call fails,
  keeps the declared schema. `note`, when there is one, goes to the planner. The core passes the
  schema on unread: `speech` uses it for the world's voices (its README, Voices), a publish connector could
  list the channels a world has keys for.
- `POST /api/worlds/<id>/connectors/test` starts the draft's server once and returns its tools,
  saving nothing. `POST /api/worlds/<id>/connectors`, the connectors page's add form (issue
  211), writes a minimal Manifest for it and installs it as a local Connector Package (see
  below; its `source` is `local` and its version `0.0.N`, reusing a local version with the same
  Manifest), enables it in the world and saves the given key values as its shared Setup values:
  each key name becomes a required, shared Credential reaching the server as that name (an
  empty value takes the one the package or `.env` holds already). It leaves a world as any
  package does, `DELETE /api/worlds/<id>/packages/<name>`. On upgrade, every connector a world
  added before packages becomes such a local package enabled for its world, its keys moved from
  `.env` to its Setup (a key a built-in also uses stays), so workflows calling it run unchanged.
- A connector stage calls the tool named like its connector (or `params.tool`) with `params`, its
  `input` (or `inputs`) and `output` artifact names and `idempotency_key` set to the stage id (`<stage id>.<n>`
  on the run's nth send-back), through the ConnectorTransport port. The call is recorded once per
  visit to the stage: a finished call is never repeated, a crash mid-job resumes polling instead
  of calling again, and a send-back that reruns the stage calls the connector again.
- A long job answers `{"job_id": ..., "poll": "<tool>"}`; swarmify calls that tool with
  `{"job_id": ...}` every 2 s until it answers `status: done` or `failed`, with optional `progress`
  (0 to 1) and `message`. A wrapper keeps job state outside its process, since every call and
  poll is a fresh MCP session.
- Every change to a call appends a `connector_call.updated` event carrying the whole call, progress
  included; `GET /api/stages/<stage id>/call` returns the raw request and response, which the run
  page keeps under a connector stage's `Technical details`.

A connector stage's call also carries `output_path` (`worlds/<w>/runs/<run>/<stage>/artifacts/<output>`,
folder created; `output` must be a bare file name), `input_path` (the newest file named like
`input` any stage of the run left; the stage fails if there is none), `input_paths` (the same for
each of `inputs`, in their order, so one connector can take a script and its voice timings),
`assets_path` (the world's assets folder, see Assets below, for the connector to read) and
`frontmatter` (world, run, stage) for a sidecar. Workflow params may not set these five. When the call is done and the
output file exists, an `artifact.created` event announces it, typed by its sidecar.

### Connector Packages

A Connector Package is a folder with a `swarmify-connector.toml` Manifest at its root and the
connector's code beside it (ADR 0019). It is installed once for the whole install (ADR 0023):
`POST /api/packages` (`{"source": "connectors/<name>"}`, a folder of this repository, or an
absolute local path; git URLs come once connectors run sandboxed) checks the Manifest, copies the
folder to `<data dir>/packages/<name>/<version>/` and runs its `install` step there with a bare
environment (PATH, HOME and uv's cache, none of Swarmify's keys), so its dependencies land beside
it. An invalid Manifest is refused with a 422 naming the field (`config[1].options: ...`); a
version already installed, or a failing step, is refused with its reason, and nothing new is
left behind. `.swarmify-install.json`, written last, records the install (its `source` and
`installed_at`); a version folder without it does not count. `GET /api/packages` lists every
installed version with its `label`, `kind`, `about` and `state` (`installed`, or `invalid` with
the `problem` when its Manifest no longer passes), and the connectors page lists them under
Installed packages.

Default packages (ADR 0023, issue 213): every package in this repository's `connectors/` folder.
On startup Swarmify installs each whose version is not installed yet, and gives it to every
world once, enabled at that version: the worlds there on upgrade, and each new world as it is
made. A world's choice after that stands (disabling one keeps it disabled through restarts), and
a newer default version installs beside the old one without moving a world's pin. The first
install of a default carries what V1 kept for it into its shared Setup: a Setting's value from
the app's environment and a Credential's from the credentials file, each by its field's `env`
name. A default that does not install is logged and left out; the app still starts. The
workflow check holds a package's stages to its Manifest: an input and an output when it
declares them, and no params outside a closed `params` schema.

Versions (issue 223): each saved workflow version records `packages`, the version of every
package it calls as its world pinned it then, and its runs use those versions, never the
world's current pin. Moving a world's pin (`PUT /api/worlds/{id}/packages/{name}`) first shows
the Manifest diff on the connectors page (`GET .../packages/{name}/switch?version=`: params,
Setup fields and permissions) and is refused while a newly required Setup field has no value; that version's Setup form
(`.../setup?version=`) fills it in before the switch.
`DELETE /api/packages/{name}/{version}` removes an installed version, refused with a 409 naming
every world that pins it and every workflow version saved with it. On startup, versions saved
before versions were recorded are stamped with their world's pins.

The Manifest, every section checked at install even where Swarmify does not act on it yet:

```toml
name = "echo"                     # lower_snake_case; workflows call the connector by it
version = "1.0.0"                 # semver; installs side by side by version
label = "Echo"                    # what the UI shows
about = "Says back what it is given, as audio."
kind = "stage"                    # tool | stage | publish
stage_types = ["connector"]       # swarm and/or connector; a publish package only connector

# Planning
planner_notes = "Use echo to voice a short line."
input_types = ["line"]            # the declared type of each input, by position; "" for untyped
world_types = "echo_types"        # a tool answering its types for one world; needs [types]

# Running
runtime = "uv"                    # uv | npm | container
command = ["uv", "run", "server.py"]   # how to start its MCP server; or url = "https://..."
install = ["uv", "sync"]          # optional, run in the installed folder
health = "echo_health"            # optional tool answering {"checks": [...]}

owned_folders = ["echoes"]        # folders of each world's assets it owns
capabilities = ["check"]          # publish | outcomes | build_session | check | remote_approvals
# primary_metric = "views"        # with the outcomes capability

[params]                          # a JSON Schema of a stage's params, with descriptions
type = "object"
additionalProperties = false
properties.loud = { type = "boolean", description = "Say it louder" }

[[inputs]]                        # and [[outputs]]: what it reads and makes
description = "the line to say"
media_type = "text/markdown"      # a media type, as audio/wav or video/*

[types.line]                      # named input types, each a JSON Schema
type = "object"
properties.text = { type = "string" }

[setup]
test = "check_setup"              # a tool called with the candidate values before saving
help_url = "https://example.org/echo/setup"

[[config]]                        # one per Setup field
key = "api_key"                   # lower_snake_case, unique
label = "API key"
type = "string"                   # string | text | number | integer | boolean | choice | url | path | oauth
secret = true                     # a Credential; otherwise a Setting
required = true
scope = "shared"                  # shared (a world may override) | world
env = "ECHO_KEY"                  # the variable it reaches the connector as; default KEY upper-cased
help = "Any value works."
# also: default, options (choice only), min, max, pattern, renamed_from,
# and for type = "oauth" a [config.oauth] table: authorize_url, token_url, scopes, pkce, client

[[config]]
key = "endpoint"
label = "Echo service URL"
type = "url"
default = "http://localhost:8099"

[[services]]                      # a program it calls that the operator runs
name = "echo_service"
url = "endpoint"                  # the [[config]] key holding its URL
health = "/health"

[permissions]                     # Credentials come from secret [[config]] fields, never listed
network = ["api.example.org"]
services = ["echo_service"]
folders = ["~/echo-cache"]
```

The full fixture is `packages/connectors/tests/fixtures/packages/echo`, and every rule has an
invalid fixture beside it in `fixtures/manifests`.

Once a world enables a package, the Connectors page draws its Setup form from the `[[config]]`
fields. Values live in `<data dir>/setup.json`, mode 600,
keyed by package, then `shared` or the world, then field key; Credentials (secret fields) are
never returned by the API, only whether they are set. For a world, a field resolves to the
world's value, else the shared one (a `scope = "world"` field has none), else its default, and a
value saved under a field's `renamed_from` key carries over. Before saving, the `[setup] test`
tool is called with no arguments in a process holding the candidate values, and answers
`{"ok": true|false, "message": "..."}`; anything but `ok: true` saves nothing and shows the
message. Every connector process gets exactly its own resolved values as environment variables
named by each field's `env`, and nothing else of the app's or another package's.

#### OAuth sign-in

A field of `type = "oauth"` is an account Swarmify signs in to for the connector. Its
`[config.oauth]` table gives `authorize_url`, `token_url`, `scopes`, `pkce` (true unless set
false), `client = "operator"` and optionally `authorize_params`, extra query parameters for the
provider's sign-in page (Google needs `{ access_type = "offline", prompt = "consent" }` to issue a
refresh token). You register your own app with the provider; its client ID and secret are two
shared Credentials the form adds before the field, `<key>_client_id` and `<key>_client_secret`,
which only Swarmify uses. Register the redirect URI the form shows,
`<the address you open Swarmify at>/api/setup/oauth/callback`: Swarmify takes it from the
request (with `X-Forwarded-Host` and `X-Forwarded-Proto` behind Caddy or Tailscale Serve), so
sign in from the same address you registered.

**Sign in** sends the browser to the provider with a one-time `state` bound to your session and
a PKCE challenge; the provider sends it back to the callback, which checks the state (once, within
ten minutes, same session) and trades the code for the token set. The token set is stored as the
field's Credential in `setup.json` (shared, or the world's own for `scope = "world"`) and is never
returned by the API. Each time the connector's process starts it gets a current access token as
the field's `env`: one with five minutes or less left is refreshed first, keeping a rotated
refresh token. A refresh the provider refuses (revoked, or the app's secret changed) marks the
field **Needs sign-in**, and so does a connector reporting an auth failure: a tool error whose
message holds the word `auth_failed`, as `raise ToolError("auth_failed: token revoked")` does in
the Python MCP SDK. Signing in again clears it. Tokens are never logged.

Google and a `*.ts.net` address: Google accepts a redirect URI for a "Web application" client
when it uses https, its host is not a raw IP, and its top-level domain is on the public suffix
list ([Google's redirect URI rules](https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation)).
`https://<host>.<tailnet>.ts.net/api/setup/oauth/callback` meets all three: `.net` is a public
suffix, and `ts.net` itself is on the [list](https://publicsuffix.org/list/public_suffix_list.dat)
(private section, submitted by Tailscale). The provider never calls the redirect URI itself, the
browser follows it, so Tailscale Serve is enough; Funnel is not needed. While the app's consent
screen is in Testing, add yourself as a test user. If a provider refuses the address, use the
loopback fallback: register `http://localhost:8000/api/setup/oauth/callback` (http is allowed for
localhost), forward the port from the device you sign in on with
`ssh -L 8000:localhost:8000 <host>`, open `http://localhost:8000` and sign in there. The token is
stored on the host either way, and refreshing needs no browser.

## Remotion render

`remotion_render` is our stdio MCP wrapper over `npx remotion render`
(`python -m swarmify_connectors.remotion`). It needs Node on `PATH` and a Remotion project; no
credentials. A relative `project` is a template by name under the data directory's `remotion/`
folder (`~/.swarmify/remotion/shorts` natively, `/data/remotion/shorts` in Docker), so a workflow
moves between hosts unchanged; an absolute or `~` path works too. A symlink there is followed, so
a template can stay in its own folder: `ln -s ~/videos/shorts ~/.swarmify/remotion/shorts`. The
workflow check reports a template that is not there. When the project has no `node_modules`, the
first render runs `npm ci` (`npm install` without a lockfile) in it, its output in the render log.

```yaml
  - connector: render
    calls: remotion_render
    input: script.md          # the script artifact an earlier stage produced
    output: short.mp4
    params:
      project: shorts           # a template in <data dir>/remotion/, or a folder path
      composition: Short        # the composition ID
      # optional: entry (src/index.ts), codec (h264), frames (0-29), props
```

The composition gets the script as input props, `{"script": <text>, "scriptPath": <path>, ...props}`
(read them with `getInputProps()`). The render runs detached from the MCP session, so a restart of
swarmify resumes polling it; progress comes from the CLI's `Rendered n/N` and `Encoded n/N` lines
and shows on the timeline. On success the video gets a sidecar, `short.mp4.md`, with frontmatter
(type video, author remotion_render, world, run, stage, created, source script, composition,
bytes, and the template's git commit when the project is a git checkout). On failure the stage
fails with the last 40 lines of CLI output, shown under the timeline. Every render, a build's
included, serves its bundle on a port the OS just handed out (`--port`), never Remotion's default
3000, where another server (an open-webui container, say) would answer and fail the render with
`window.getStaticCompositions is not defined`.
Tests use a fake `npx`; one real render runs when you opt in:
`SWARMIFY_REMOTION_PROJECT=<project> SWARMIFY_REMOTION_COMPOSITION=<id> uv run pytest -k real_render`.

## Remotion build

Swarm agents have no shell (ADR 0018), so a video Swarmify makes itself comes from one built-in
stage connector, `remotion_build`: one coding session writes a Remotion project for the run's
script, then the render code above, not the agent, turns it into the stage's video.

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

The stage copies `base` (else a minimal starter shipped in the repo,
`packages/connectors/src/swarmify_connectors/remotion_starter`, composition `Main`), without its
`node_modules` or `.git`, into a fresh `<stage folder>/build/`, and runs one session there as
agent `builder` with Bash, Read, Write, Edit, Glob and Grep, the script path (the first of
`inputs`, whose other paths the prompt names too), the world's assets by path (when it has any,
to copy into `public/`), the brief and the run's gate decisions as required reading. It is logged, costed against the run's Budget and
stopped by it like any agent; failing twice, or running out of time, fails the stage with the
builder's last log lines. Then the render runs as a `remotion_render` call on the build folder
(its call shows on the timeline as usual, a failed render fails the stage with the CLI's last
lines), the video gets its sidecar and becomes the stage's artifact, and the build folder stays
for a person to open, without its `node_modules` (`npm ci` brings them back).

A send-back that reruns the stage revises, it does not start over: the last build is kept as
`<stage folder>/build.v<n>/` (n counting the run's visits from 1) with the video it rendered beside
it as `build.v<n>.mp4`, `build/` starts as a copy of it, and the prompt opens with "This is a
revision": the send-back note (when the send-back named this stage) and the previous version are
required reading, change what the note asks and keep the rest. Both stay inside the stage folder,
which the session may read. A stage never run, or whose last render failed, starts fresh.

When one of `inputs` is a `timings.json` (the speech package's), the brief turns voice
first, as the spike on branch `spike/voice-first-video` proved: copy the timings and their takes
into `public/`, take the composition's length from the voice timeline, play each line as a trimmed
`<Audio>` in a `<Sequence>` spaced by its pause (scaled for the style, none after a
`joined_next` line), page karaoke captions per line with `createTikTokStyleCaptions` and
`white-space: pre`, and duck a music bed from the world's assets under speech. The prompt carries a
sample composition doing all of it,
`packages/swarm/src/swarmify_swarm/voice_first.tsx`, which a test type-checks in the starter. The
starter ships `@remotion/captions`.

The session's sandbox:

- Bash runs in the Agent SDK's OS sandbox (Seatbelt on macOS, bubblewrap on Linux), with
  `allowUnsandboxedCommands: false` and `failIfUnavailable`, so a command never runs outside it.
  Writes reach only the build folder and the npm cache; the data directory is unreadable except
  the build folder, the npm cache and the stage's own world (the script, decisions and assets); the
  network reaches only `registry.npmjs.org`, `fonts.googleapis.com` and `fonts.gstatic.com`.
- The PreToolUse hooks enforce the same paths without the sandbox: Write and Edit only inside
  the build folder, Read, Glob and Grep only inside it or the world's folder (resolved, so `..`
  and a symlink pointing out are outside), never a `.env`; a Bash command asking to leave the
  sandbox is refused, and one naming the data directory or a `.env` file too (a tripwire; the
  sandbox is the wall).
- The environment carries no keys: every name in the credentials file or declared by a
  connector, and `SWARMIFY_PASSWORD`, is blanked for the session, and the sandbox also unsets
  them and the model's own credentials for every command. The world's skills are not loaded
  (Claude Code refuses writes inside a plugin's folder, and the build folder is in the world's).
- `npm install` reuses one cache, `<data dir>/npm-cache`, so a run does not download everything.

The render executes the agent's code (its `remotion.config.ts`, its `node_modules`), so a project
under the data directory's `worlds/` renders in an OS sandbox of its own: `sandbox-exec` on macOS,
`bwrap` on Linux, and no render where neither exists. It has no network beyond loopback (fonts and
images must be files in `public/`, which the builder is told), reads nothing of the data directory
but the project, the output folder and the shared browser, and writes only those and temp. It
never installs: a build that left no `node_modules` fails. Chrome Headless Shell comes from `PATH`
(the Docker image) or is downloaded once by Remotion into `<data dir>/chrome` from the starter's
pinned packages. On macOS that profile starts from allow-all and denies network, the data
directory, writes, other processes, Apple Events and Launch Services, since Chrome needs Mach
services a deny-all profile refuses; Linux unshares every namespace.

Tests drive the builder with the scripted CLI and the render with a fake `npx` inside the real
sandbox. One real build, on your subscription and the real Remotion CLI, runs when you opt in:
`SWARMIFY_REAL_BUILD=1 uv run pytest apps/api/tests/test_build_http.py -k real -s` (about a
minute on a Mac, most of it the first npm install and the Chrome download).

## Speech

`speech` is a default Connector Package, `connectors/speech`: Swarmify installs it on startup and
enables it for every world, so a workflow's `calls: speech` works with no setup. Its README covers
its Setup (the speech engines, a Setting on the Connectors page, carried over from V1's
`SPEECH_ENDPOINTS`), how to run the engines (Kokoro and Chatterbox, its own compose file), the
script format and the voice catalog.

### Voices on the Assets page

A world's voice catalog is `assets/voices/voices.json` with its clips (the speech package's README,
Voices).
- On the world's Assets page (sidebar, or More on a phone) every voice is a card you can play,
  with its clip length, tags, engine and the saved workflows whose speech step names it (`params.voice`,
  current versions). "Add a voice" uploads a clip (any audio the browser reads, saved as a 16-bit
  mono WAV) or records one with the microphone (stopping at 10 s), and needs a name, a
  description and the consent line; Edit changes the description and tags, Delete drops the
  entry and its clip after naming the workflows that lose it. Over HTTP:
  `GET /api/worlds/<id>/voices` (the catalog as the connector reads it, each with `clip`,
  `seconds` and `used_by`, plus the warnings for ignored entries), `POST /api/worlds/<id>/voices`
  (multipart: `name`, `description`, `tags` comma separated, `consent`, `clip`; refused with the
  reason unless the clip is a 16-bit WAV of 5 to 10 s and the name is new),
  `PATCH /api/worlds/<id>/voices/<name>` (`{"description", "tags"}`) and
  `DELETE /api/worlds/<id>/voices/<name>`. Each change rewrites voices.json in one step
  (`swarmify_connectors.voices`, with the speech package's rules, as its seeding writes it),
  keeping entries it does not touch as they are.

## Publishing

A publish connector posts to the outside world, so a publish stage runs only right after a gate
(ADR 0007, no override). The editor refuses to save a workflow that breaks this, and the runner
refuses to execute such a stage too (it fails the stage), which catches a connector that became
publish after the workflow was saved. The gate holds the rule; the connector says what goes out.
The built-in `youtube` and `upload_post` take these params:

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
      metadata: script.md              # optional: take title, description, hashtags and tags from its frontmatter
      hashtags: [owls, nature]         # optional, at most 5, one word each, `#` optional
      tags: [owls, owl facts]          # optional, YouTube's keywords, 450 characters in all
      visibility: public               # public, unlisted or private (default private)
      schedule: "+1d 09:00"            # optional, see below; needs public
      made_for_kids: false             # YouTube's COPPA declaration (default false)
      user: my-profile                 # upload_post only: the Upload-Post profile
      platforms: [youtube, tiktok]     # upload_post only, default [youtube]
```

`metadata` names a file an earlier stage of the run left (the newest of that name, like `input`);
the core hands its YAML frontmatter to the connector, and for the built-ins the `title` and
`description` there (and `hashtags` and `tags`) win over the params, which stay the fallback, so a daily workflow posts each video under its own script's title. A missing file is a
problem the gate shows. `schedule` is ISO 8601 with a timezone, or relative: `09:00` (the next
09:00), `+1d 09:00` (tomorrow at 09:00), either with an IANA timezone after it
(`+1d 09:00 Europe/London`, UTC by default). A relative schedule is resolved when the gate is
shown, so a scheduled workflow posts every run. `made_for_kids` goes to YouTube as
`selfDeclaredMadeForKids` (to Upload-Post as `youtubeSelfDeclaredMadeForKids`); content made for
children must set it.
`hashtags` go out as the description's last line (`#owls #nature`), where YouTube shows up to
three above the title; a hashtag with a space or punctuation, more than five, or a description
over YouTube's 5,000 bytes with them is a problem the gate shows. `tags` go to YouTube's tags
(`snippet.tags`, Upload-Post's `youtubeTags`) beside the retry tag, which is why they stop at 450
of YouTube's 500 characters. Both are editable at the gate. Writing them is the workflow's job,
for example the script stage's frontmatter; Swarmify never makes them up.

The gate before a publish stage carries `publish` in `GET /api/gates` (and MCP `list_gates`,
and the `gate.opened` event): the connector, its `fields` (each a `key`, `label`, `value`,
`editable` flag and optional `hint`, as the connector describes them; for the built-ins Title,
Description, Platforms for Upload-Post, Visibility, Made for kids, When and Video) and any
`problems` that would stop the post. The gates inbox shows them under the ask, with the editable
ones as fields of the form, and disables "Confirm and post" and "Approve" while there are
problems; the server refuses those answers too (409 naming the problems), so a broken publish
stage is sent back or aborted, never reached. Answering the gate (`POST /api/runs/{id}/decision`,
optionally with `edits`, new values for editable fields by key, and `seen`, the values it showed)
records every field's value in the decision (its `publish` field and `decision.md`), refusing an
edit to a field that is not editable or that would stop the post, and a field that changed since
it was shown (a relative schedule shown at 08:58 as `09:00` today resolves to tomorrow at 09:01),
which the inbox then shows again; the publish stage sends exactly those values. A publish call is attempted at most once per visit to the stage: a call
interrupted by a crash is never repeated, it fails the stage asking you to check the platform.
After the call, `publish.md` (type `publish`, with the fields `sent`, `links`, `status` and the
post or job `id` in its frontmatter, and the fields and links again in its body) lands in the stage's `artifacts/` folder.

A world's own publish connector (kind `publish` on the Connectors page) answers two MCP tools:

- `preview` gets `inputs` (the stage's input file names), `params` (the stage's, less
  `metadata`), `metadata` (the frontmatter of the file `params.metadata` names, else `{}`) and
  `edits` (by field key; apply only those to fields you mark editable), and answers
  `{"fields": [{"key", "label", "value", "editable", "hint"}], "problems": [...]}`. It is called
  when the gate opens, when it is answered, and before the post; a call that fails is a problem
  the gate shows. It is not called when the workflow is saved.
- `publish` gets `fields` (every value as confirmed, by key), `idempotency_key` and
  `input_path`/`input_paths`, posts exactly those values, and answers `url` or `links` (platform
  to URL), and optionally `id` and `status: scheduled`.

The built-ins answer the same two calls in process (`swarmify_connectors.builtin_publish`)
until they move into their own packages.

- `youtube` is our wrapper over the YouTube Data API (`swarmify_connectors.youtube`, stdio MCP).
  It uploads with `videos.insert`; with a schedule the video goes up private with `publishAt` and
  YouTube makes it public at that time. Each upload is tagged `swarmify-<idempotency key>`, and before
  uploading the wrapper looks for that tag among the channel's latest 50 uploads (3 quota units),
  so retrying a failed publish stage answers the video an earlier attempt already put up instead
  of posting it twice. Setup, once: create a Google Cloud project, enable the
  YouTube Data API v3, create an OAuth client of type Desktop app, then run
  `YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... uv run python -m swarmify_connectors.youtube authorize`,
  allow the upload and read-only scopes in the browser, and store the three keys (`YOUTUBE_CLIENT_ID`,
  `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`) with "Add keys" on the connectors page. By
  default uploads have their own quota of 100 a day; the retry check and the other reads share
  10,000 units a day ([quota costs](https://developers.google.com/youtube/v3/determine_quota_cost)).
- `upload_post` is Upload-Post's official MCP (`npx -y @upload-post/mcp@0.11.4`, needs Node),
  adopted as is (ADR 0015) with `UPLOAD_POST_API_KEY` from app.upload-post.com. Swarmify calls its
  `upload_video` tool with the call's idempotency key. Posting now records
  each platform's link (a platform that failed is listed in the artifact; when none succeeded the
  stage fails). An upload over about 59 seconds moves to Upload-Post's background and answers a
  request id; the stage then polls `get_status` with it until the upload finishes and records the
  links the same way; a request id Upload-Post no longer knows fails the stage. A scheduled post records the Upload-Post job id; its links appear only once it
  goes out; reading those links back is not built yet.

### Outcomes

Every six hours the API asks the `youtube` connector's `views` tool (`videos.list`, part
statistics, 50 ids per call) for the view count of every video a publish artifact names: a
`youtube` post's video id, and any YouTube link, whichever connector posted it. A changed count
is written into that `publish.md`, as `views` and `views_at` in the frontmatter and as an
`## Outcome` section in the body (replaced each time), because graphify reads bodies only; each
write appends `outcome.recorded`. The run timeline shows the count on the publish card. A world
whose `youtube` keys are missing is skipped. The read needs the `youtube.readonly` scope, so a
refresh token made before view counts existed must be authorized again (the `authorize` step
above now asks for both scopes).

## Retention

Each world keeps its run binaries (videos, images, anything under `runs/` that is not markdown)
for `retention_days`, 90 by default, 0 for forever: `PATCH /api/worlds/<id>` with
`{"retention_days": n}`, or the world's Settings page. Every six hours, alongside the outcome
write-back, files unchanged for longer are deleted. Markdown is never deleted: each deleted
file's sidecar (written if it had none) gains `deleted` in its frontmatter and a line in its
body, so the graph and the run timeline keep the record, and each deletion appends
`artifact.pruned`. `create_app(upkeep_seconds=...)` sets the interval.

Tests never reach either service: the fake ConnectorTransport stands in for both MCP servers,
and the YouTube wrapper's own tests answer its HTTP requests with httpx's MockTransport.

## Whiteboard

Each swarm stage has a whiteboard of tasks after the beads model (ADR 0017): a task has a status
(open, claimed, done), an owner, dependencies on other tasks of the stage, a heartbeat, notes and
who added it. A task is claimable once every task it depends on is done. A claim is one
`BEGIN IMMEDIATE` transaction updating the row only while it has no owner, so of any number of
concurrent claims exactly one wins and the rest get a 409. The owner heartbeats, optionally with
partial notes; a swarm agent's session hooks also renew its claims on every tool call and model
step, so it need not heartbeat itself. A claim whose last heartbeat is older than two minutes
returns to open with its notes kept and a note saying why (the API sweeps every 15 seconds), so a
dead session's claims go back to the board. Every change appends a
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

A swarm stage runs the world's Cast (see Settings and cast below) through the AgentRuntime port:
the planner, then one worker per ready task up to the world's agent count (`worker-1` to
`worker-n`; Worker 1 to n on the page, or Gilfoyle, Dinesh, Jared and Big Head in the Pied Piper skin), each in its own thread over the
shared whiteboard, then the judge. The adapter is the Claude Agent SDK, whose wheel bundles the
`claude` CLI; sessions run on your Claude subscription (ADR 0010), so for a headless host run
`claude setup-token` once and export `CLAUDE_CODE_OAUTH_TOKEN`. The planner seeds an empty
whiteboard, the workers work it, and the judge decides when the stage is done (see Planner and
judge below). An agent's standing prompt from the cast is added to its system prompt and its
cast tools narrow the stage's `tools`.

Models: a session runs on the most specific choice of a swarm stage's `model` (all its agents),
the Cast's model for the role (`planner_model`, `worker_model`, `judge_model`, `retro_model`),
the world's `model`, then the global default: `SWARMIFY_MODEL`, else `claude-opus-5-5`. The
planner chat runs on the world's model. `GET /api/models` lists the priced models and the global
default. The model a session reports at its start is stored on the agent (`model`) and logged, and
the run page shows it beside each agent.

- In-process tools (`mcp__swarmify__<name>`): `query_graph`, `list_tasks`, `claim_task` (the named
  task or the next ready one), `heartbeat`, `finish_task`, `add_task`, `list_assets` (the
  world's assets by path, read-only), `write_note`, `write_artifact`. Notes land in `worlds/<w>/runs/<run>/<stage>/notes/`, artifacts in
  `.../artifacts/`, each as markdown with frontmatter (a non-markdown artifact gets a `.md`
  sidecar), and each write appends an `artifact.created` event. Graph tools (`query_graph`,
  `get_node`, `get_neighbors`, `god_nodes`, `shortest_path`, `graph_stats`) are described under
  Graph below.
- Allowlist: the tools above, `Skill`, `Read` (inside the world folder only), plus per stage
  the Agent Tool `web_search` (WebSearch, WebFetch) and any connector named in `tools` (`mcp__<name>__*`, its
  server and credentials passed to the session).
- Hooks: a PreToolUse hook writes every tool call to the stage's live log, denies anything outside
  the allowlist and denies a claim until the agent has queried the graph. Each step's estimated
  cost (not billing) is added to the agent and checked against the Budget (see Budget and
  baseline); the run's `spent_usd` is the sum over it and its child runs.
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

- GraphWriter (Anton in the Pied Piper skin, ADR 0003) is the only writer. It polls the world folders every
  2 s, waits until a world has been quiet for 30 s after its last change, then runs
  `python -m graphify extract <world> --backend claude-cli --exclude graphify-out/ --exclude
  assets/` (assets are files, not knowledge, so adding one triggers no rebuild either). One thread
  does every rebuild in turn: serialized per world, queued across worlds. A queued world is
  snapshotted as its rebuild starts, so changes made while it waited are absorbed by that one
  rebuild rather than queueing another. Each rebuild appends `graph.rebuilt` (`nodes`, `edges`,
  `nodes_added`, `edges_added`, `seconds`, `input_tokens`, `output_tokens`) or `graph.failed`
  (`error`, `seconds`, `shrink`) to the world's stream. `shrink` means graphify's shrink guard
  refused a semantic shrink (a file re-extracted to fewer nodes than before). A refusal over failed
  chunks is a plain failure, `shrink` false, since the next rebuild retries them. A shrink keeps
  refusing until it is accepted, which appends `graph.shrink_accepted` and reruns that world at
  once with `--allow-partial`, once. A deleted world leaves the queue; a rebuild still running
  when its world is deleted records nothing and removes the folder graphify recreated. It runs
  inside the API process; with
  `SWARMIFY_GRAPH_WRITER=external` the API leaves it to a `swarmify-graph-writer` process instead
  (the compose file does this). graphify's Claude CLI backend needs `claude` on PATH and logged in.
- GraphIndex port: `McpGraphIndex` runs one `python -m graphify.serve` stdio process for every
  world, picking the graph per call with `project_path`; `FakeGraphIndex` answers from the graph
  file for tests.
- Agents' graph tools take an optional `world`: their own world by default, or a world the stage
  lists under `graphs` (by name or id). Any other world or path is refused, and every answer is
  labelled with the name of the world it came from, `[world Market]` (ADR 0009).
- Cross-world access is read-only by construction: the write tools (`write_note`,
  `write_artifact`, `add_task`) take no world and always land in the stage's own world, and
  Write, Edit and Bash are outside every allowlist.
- `GET /api/worlds/<id>/graph` returns size, god nodes, `communities` (graphify's largest
  clusters, each with its most connected nodes) and `surprises` (graphify's surprising
  connections, across notes or clusters), each node with the `source_file` it came from and the
  `run_id` that wrote it, the last rebuild (with its estimated
  `cost_usd`) and the last failure, whether an accepted shrink is still rebuilding (`accepted`),
  and `spent`: every rebuild's count, seconds and estimated cost, refused ones included. Cost is
  priced from graphify's token counts at the rates of `GRAPHIFY_CLAUDE_CLI_MODEL` (the dearest
  model when unset), so it errs high; the Claude CLI backend itself bills the plan.
  `POST /api/worlds/<id>/graph/accept-shrink` accepts a shrink (202, or 409 when the last rebuild
  was not a shrink refusal). `POST /api/worlds/<id>/graph/query` (`{"question": ...}`) asks the
  graph. The page is `/worlds/<id>/graph` (Middle-out): the query box, god nodes, communities
  and surprising connections, each node a question to ask with links to its run and its note.
  It shows a shrink refusal with an "Accept the shrink" button, and reloads on `graph.rebuilt`
  and `graph.failed`.
- Real graphify runs only in the opt-in test, which spends Claude subscription tokens:
  `SWARMIFY_GRAPHIFY=1 uv run pytest packages/memory/tests/test_graphify_live.py --no-cov`.

In Docker the graph writer is its own container; see Deployment.

## Planner and judge

A swarm stage runs three kinds of agent session on the same runtime: `planner` (Richard in the
Pied Piper skin), `worker-1` (Gilfoyle) and `judge` (Erlich). Planner and judge are short sessions that may
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
4. **Project.** On a pass with an `output_type` (`idea`, or `list[idea]` for several, or a type
   a connector declares, see Typed contracts under Connectors), each item
   becomes `artifacts/<type>-<slug>.md` with frontmatter `type`, `author`, `run`, `stage`, `title`,
   `rank`, `reason` (why the judge ranked it so, shown on the pick card), its fields (an idea's
   `hook` and `why`) and `sources: ["<note path>#p<n>"]`, keeping only
   references to a real paragraph of one of the stage's notes, quoted under Sources in the body
   (ADR 0011). A single type keeps the best ranked item.

A retried stage resumes from what is stored: a seeded board is not planned again, a worked board is
not worked again, a passing verdict is projected again from its stored items without a new judge
session. `GET /api/runs/<run>/stages/<stage>/verdicts` lists the verdicts and
`GET /api/runs/<run>/stages/<stage>/artifacts` the stage's artifact files with their frontmatter;
the run page shows both under the timeline, live.

Scripts can answer for a session with `Answer(data)` (the structured output) and refer to the task
just claimed with `LAST`; `Script(..., cycle=True)` repeats the step lists per run.

## Settings and cast

Each world has five settings on `/worlds/<id>/settings`, read with `GET /api/worlds/<id>` and
changed with `PATCH /api/worlds/<id>` (any subset of `monthly_budget_usd` (null removes it),
`run_cap` (1 to 8), `retention_days` (0 to 3650, 0 keeps binaries forever), `agent_count` (1 to
8), `model` (a model id, null for the global default), and the per-role `<role>_model` the cast
page sets); every change is a `world.updated` event. The monthly budget caps each new run's Budget (see
Budget and baseline), the run cap is the world's DBOS queue concurrency and applies at once, the
retention is read by the retention job, and the agent count is how many workers the cast has.

The Cast is the world's agents: `planner`, `worker-1` to `worker-<agent_count>` and `judge`
(Planner, Worker 1 to n and Judge on the page, ADR 0004). The Pied Piper names (Richard,
Gilfoyle, Dinesh, Jared, Big Head and Erlich, with Anton for the GraphWriter, Russ for the Budget
and Hooli for a baseline) are an opt-in local skin: Settings, Names, "Pied Piper names", kept in
that browser only. `GET
/api/worlds/<id>/cast` lists them at their current version with a record folded from the world's
event stream, no extra storage: tasks done (`task.updated`), stages passed on the judge's first
verdict out of stages judged (`verdict.created`), send-backs to stages the agent worked
(`run.updated` decisions) and estimated cost (`agent.updated`). `PUT /api/worlds/<id>/cast/<name>`
with `{"prompt", "tools", "confirm": true}` appends a version of the agent's standing prompt and
allowed stage tools (`tools` null means any the stage allows); it is refused without `confirm`
or when nothing changed, and `GET /api/worlds/<id>/cast/<name>/versions` lists every version,
version 1 being the default. The page is `/worlds/<id>/cast`: each edit asks for confirmation
naming the version it becomes, and applies to every stage that starts from then on. The page also
picks a model per role (planner, worker, judge, retro), over the world's (see Agents).

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
  lists it under `forks`. The run page offers "Fork from here" on a selected finished stage (and a
  failed run's status line on the stage that failed), so a
  pick gate can be answered differently without redoing the research.
- **Promote.** `POST /api/workflows/<id>/promote` (`{"artifact": "<path>", "stage": n, "version":
  optional}`) starts a run at stage `n` with a Library artifact as its input: stages before `n`
  are inserted `skipped`, the file (with its sidecar, frontmatter kept as provenance) lands in the
  last skipped stage's `artifacts/` folder where later stages' `input` finds it (renamed to stage
  `n`'s `input`, or its one `inputs`, when it names one, so `script-the-chase.md` reaches a render
  that takes `script.md`), and the run carries `promoted_from`. A stage with several `inputs` is
  refused (422): one file cannot stand in for them. `n` must skip at least one stage and start at or before the last (422). The
  Library's "Promote..." picks the workflow and the stage.

### Assets

Each world has `worlds/<id>/assets/`: the operator's reusable files, such as music beds, sound
effects and logos. Nothing is seeded; a new world's folder is empty. Add files on the world's
Assets page (Files, "Upload files"), with `PUT /api/worlds/<id>/assets/<name>` (the body is the
file, `name` a plain file name; the same name replaces it) or by dropping them into the folder,
subfolders included. `GET /api/worlds/<id>/assets` lists them by path (`assets/<name>`) with their
size, and `GET /api/worlds/<id>/files/<path>` serves one, as it serves run files. The page previews
each (an image's thumbnail, an audio player, a video's first frame), and renames
(`PATCH /api/worlds/<id>/assets/<path>` with `{"name"}`, in the same folder, never over another
file) or deletes one (`DELETE /api/worlds/<id>/assets/<path>`). Both refuse paths outside
`assets/` and anything in `assets/voices/`: voices change only through the voices endpoints.

Agents never write there. Swarm agents list them (`list_assets`) to name one in a script; a
connector stage gets the folder as `assets_path` (the speech package alone gives `voices/` a
meaning, see Voices on the Assets page); the `remotion_build` session reads them (its
sandbox and hooks allow reads in the world's folder and writes only in the build folder) and is
given their paths to copy into `public/`. Nothing about them is video-specific, and they are
kept out of the graph.

Free CC0 (public domain, no credit needed) beds and effects: [Freesound](https://freesound.org)
with the "Creative Commons 0" license filter, [Kenney's audio packs](https://kenney.nl/assets/category:Audio),
[OpenGameArt](https://opengameart.org) filtered to CC0, and the [Free Music
Archive](https://freemusicarchive.org) filtered to CC0. Check each file's license on its page.

## Learning: retro, skills, scorecard

Every run but a baseline ends with a Retro stage (ADR 0008): a judge-style session that reads the
run's verdicts, send-backs and gate notes, the skills the agents load and the earlier retros, and
answers with at most one Skill Proposal (a new, changed or retired skill, with the evidence lines
that call for it) or nothing. The retro runs through the swarm's session seam, so it is logged,
costed against the Budget and resumable like any agent (named `retro`). A proposal is written as
`artifacts/proposal.md` of the retro stage (type `skill_proposal`, in the Library) and the stage
waits at a gate of kind `skill`; a quiet retro just finishes the run.

A run that fails (not one aborted) still runs its Retro, since failed runs have the most to teach:
the stages between the failed one and the Retro are skipped, the Retro reads the failed stage's
error and the last 30 lines of its log, and the run ends failed once the Retro finishes or its
proposal is decided.

- `GET /api/runs/<id>/proposal` is what the gate shows; `POST /api/proposals/<id>/decide` with
  `{"approve", "content", "apply_workflow", "note"}` approves it (writing `content`, the SKILL.md
  as edited, as the next version of the world's skill) or rejects it with `note` as why, then
  answers the run's gate (`POST /api/runs/<id>/decision` refuses a skill gate with 409). The
  proposal is decided before anything is written, so a refused or repeated decision writes no
  skill and no workflow version. The gates inbox renders the proposal with its evidence, the editable
  SKILL.md and Approve and Reject.
- Skills: a world's own live in `worlds/<id>/skills/<name>/SKILL.md`, every version in
  `skill_versions` with the run that produced it (`GET /api/worlds/<id>/skills`, each with
  `source`, `version` and `run_id`; `GET .../skills/<name>/versions`). The shared folder
  `<data dir>/skills/<name>/SKILL.md` is read by every world: before agents start, each shared
  skill the world has not overridden is linked into the world's folder, so the plugin loads it
  (ADR 0016) and a world skill of the same name wins. The cast page lists them; a world skill's
  History lists its versions, and Restore writes an older version's content as the next version
  (`POST /api/worlds/<id>/skills` with note `Restored v<n>.`), so the rollback is in the history too.
- Promote: the Library's "Make a skill..." turns any note or artifact into a SKILL.md draft
  (title as description, body as the procedure) you edit and save with
  `POST /api/worlds/<id>/skills` (`{"name", "content", "run_id", "note"}`), version 1 or the next.
- Workflow Suggestion: when the same manual intervention (a send-back from one gate to one
  stage, the same note at a gate, or a task you added with the same title) recurs in three runs
  of a workflow, the retro gets the recurrence and the pinned YAML and may answer with the whole
  YAML changed; the gate shows it and `apply_workflow: true` saves it as the workflow's next
  version (refused with 422, nothing decided, when it breaks a rule).
- Scorecard: `GET /api/workflows/<id>/scorecard`, a read model over the world's events with no
  storage, lists every run of the workflow oldest first with first-try acceptance (stages passed
  on the judge's first verdict out of stages judged), send-backs, estimated cost, duration and
  outcome (the published artifacts' latest view counts). The page is
  `/worlds/<id>/workflows/<wf>/scorecard`, linked from the editor and the world overview, with a
  trend line comparing the last three swarm runs with the ones before.
- A child run a workflow stage starts has no Retro of its own, and the parent's Retro reads only
  the parent's verdicts, so a child's send-backs reach no retro yet.
- `create_app(retro=False)` leaves the Retro stage out; tests that script no retro session use it.

## Eval: baseline against swarm

The swarm evidence eval (ADR 0020): for which kinds of task does a swarm earn its cost over one
agent? `swarmify-eval` runs fixed tasks (in `swarmify_learning.evals`), each naming the starter
workflow it exercises: research briefs (heat pumps in cold climates), drafts (a beginner's guide
to composting), typed lists from the scheduled monitor (AI regulation) and the weekly short (home
espresso). The first three are the task kinds, 8 tasks each; one eval runs one kind, each task k
times on the baseline arm (one agent a swarm stage) and k times on the swarm arm (3 agents a
stage), following Anthropic's agent evaluation guidance. It is separate from the test suite and
spends real model time, so it runs only when you ask:

```sh
mise run dev:api                                      # the API with the real model, in one shell
uv run swarmify-eval --kind research-briefs           # 8 tasks, 2 trials an arm; prints the verdict
uv run swarmify-eval --kind typed-lists --tasks 2 --k 1   # a smoke run
```

`--kind` is `research-briefs`, `long-form-writing` or `typed-lists`.

Options: `--api` (default `SWARMIFY_BASE_URL` or `http://localhost:8000`, signing in with
`SWARMIFY_PASSWORD` when set), `--budget` (USD a trial, default 5, since the swarm's research stage alone
spends about 3), `--timeout` (seconds a trial, default 2700) and `--clean` (delete the trial
worlds afterwards).

- Fresh world per trial: each trial creates its own world (`Eval <task> <arm> <n>`), saves the
  eval workflow there and starts one run, `{"agents": 1}` for the baseline arm and `{"agents": 3}`
  for the swarm, so the arms differ only in their workers a stage. The eval
  workflow is the task's starter on the task's topic with no gate, schedule, render or publish,
  so a trial never posts anything and never waits on you.
- Outputs are graded, not the path, and each must name the task's topic: one of its topic words
  or listed synonyms, matched by word stem (so "biking" names "bike"). A research brief's
  `brief.md` must be 250 to 1000 words citing at least 3 distinct links; a draft needs
  `outline.md` and a `draft.md` of 500 to 1200 words; a monitor's typed list must project 3 to 10
  `finding-*.md` items, each with a source link; the weekly short's `ideas.md` must rank at least
  5 ideas as a numbered list and its `script.md` be 60 to 200 words. A trial is accepted when its
  run is done and every check passed. A retro proposal is rejected so the run can end; a spent
  budget or the timeout aborts the trial, which then fails.
- A blind pairwise judge then decides quality, since both arms usually pass every check: after
  each pair of trials (the same task and trial number on each arm) the harness calls
  `POST /api/evals/judge` (`{"task_id", "index", "baseline": {"world_id", "run_id"}, "swarm":
  {...}}`), and one judge session sees what each side's workflow left (the brief, the outline
and draft, every finding, or the ideas and script) as A and B, arm
  labels removed and the order shuffled per task (seeded by the task id, so it is the same every
  run). It answers A, B or tie with a reason; a pair it fails to judge is left undecided.
- The report gives, per arm, first-try acceptance, pass at k (any of a task's k trials passed)
  and pass to the k (all k passed), averaged over tasks, the mean estimated cost a trial, the mean
  time to an accepted output (the Retro stage left out), and the judge's wins, losses and ties
  with the cost a win (the arm's whole cost over its wins). A kind is a swarm win only past a bar
  fixed in advance: the judge prefers the swarm in more than 60% of the decided pairs (ties left
  out), that share is significant (one-sided sign test, p under 0.05), and the swarm's cost a win
  is at most 3 times the baseline's. The verdict sentence says so with the wins, p, the cost a
  win, acceptance, time and cost a trial.
- `POST /api/evals` (`{"k", "kind", "trials", "judgements"}`) summarizes and stores the report as `report.md` (type
  `eval`, the whole report in its frontmatter, tables in its body) in the `Evals` world, made on
  the first report, so it shows in that world's Library and graph. `GET /api/evals` lists the
  reports newest first; the web skin shows them at `/evals`, linked from the worlds page, with
  each kind's newest verdict first, then every report with the judge's picks and reasons, and each trial linking to its run.

Tests drive the harness against the real app with the scripted CLI behind the Agent SDK, so the
suite checks the whole loop with no model.

## Sign-in

One operator account per install. Its password is `SWARMIFY_PASSWORD`: at every start the api
stores a scrypt hash of it (a salt of its own, in `swarmify.db`) unless the stored hash already
matches, so changing it in `.env` and restarting changes the password and signs every browser
out. Once stored, the variable may be dropped natively and the password stays; with none set or
stored there is no sign-in, which is right only on `127.0.0.1`.

- The skin shows a sign-in page that asks only for the password. `POST /api/session`
  (`{"password"}`) sets the session cookie, a random id signed with the install's secret, valid
  30 days on that browser: HttpOnly, SameSite Lax, and Secure when the request came over https
  (directly or with `X-Forwarded-Proto: https` from a proxy). `GET /api/session` says whether
  the browser is signed in; `DELETE /api/session` signs out, which ends that session on the
  server too. Ten wrong passwords within 15 minutes are answered 429 with `Retry-After` until the
  oldest leaves the window.
- Every other `/api` path and `/mcp` answers 401 without a session; the skin's static files and
  `/api/health` stay open.
- API tokens are for the MCP server and scripts, so tools never hold the password. The API
  tokens row of any world's Settings page (tokens are the install's, not the world's) creates
  one with a name and shows it once (`POST /api/tokens`); only its SHA-256 is kept. The Sign-in
  row below it signs the browser out. `GET /api/tokens` lists them without secrets,
  `DELETE /api/tokens/<id>` revokes one at once. A token opens only the MCP server over HTTP,
  `/mcp` (Streamable HTTP, stateless, the same tools as `swarmify-mcp`), sent as
  `Authorization: Bearer <token>`; the rest of the API refuses it. For Claude Code:
  `claude mcp add --transport http swarmify http://<host>:8000/mcp --header "Authorization: Bearer swm_..."`.
- Upgrading from basic auth needs no step: the `SWARMIFY_PASSWORD` already in `.env` becomes the
  operator password on the first start, and the browser asks for it once on the new sign-in page
  (no user name; `SWARMIFY_USER` is no longer read and may be removed).

### Reaching an install over Tailscale

Tailscale puts your devices on a private network, so a phone away from home reaches the install
without opening a port on the router.

1. Install Tailscale on the host and on each device you use, signed in to the same tailnet
   (`curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up` on Debian; the
   app on a Mac or phone).
2. The install is now at `http://<host name>:8000` from those devices (MagicDNS), as on the LAN.
3. For https, turn on HTTPS certificates in the tailnet's admin console and run
   `sudo tailscale serve --bg 8000` on the host: the install is at
   `https://<host name>.<tailnet>.ts.net` with a real certificate, and the session cookie is
   Secure there. `tailscale serve status` shows it, `tailscale serve --https=443 off` stops it.
4. To keep it off the LAN, set `SWARMIFY_BIND=127.0.0.1` in `.env`: only `tailscale serve` on the
   host reaches it then.

Do not use `tailscale funnel`, which publishes the install to the whole internet. Set
`SWARMIFY_BASE_URL` to the address you open, so Slack links point there.

## Deployment

The same `compose.yaml` runs on the Mac (Docker Desktop) and on a Debian VM (Docker Engine with the
compose plugin, `apt install docker.io docker-compose-plugin` or Docker's own apt repo). `web`
(Caddy, `docker/Caddyfile`) serves the built skin and proxies `/api`, event streams unbuffered,
to `api`; it is the only service with a published port. `api` (API and runner) and `graph-writer`
(the GraphWriter, ADR 0003) share one volume, `data`, mounted at `/data`. The compose project's network is named `swarmify`,
which a package's own compose file joins for the Services it runs (see Speech). The skin has its own image, so a UI-only change rebuilds and
restarts only `web`: runs in the api (an agent session, a Remotion build) carry on. One image
serves `api` and `graph-writer`. It holds Node and `npx`
for the Remotion render connector, Chrome Headless Shell downloaded by Remotion's own
`remotion browser ensure` with the shared libraries Remotion's Docker guide lists, and the Claude
Code CLI the Agent SDK bundles, linked to `/usr/local/bin/claude` for graphify.

1. On any machine where you can open a browser and are logged in to Claude Code, run
   `claude setup-token`. It prints a one-year OAuth token for your Pro, Max, Team or Enterprise
   subscription and does not save it. It is for your own use only (ADR 0010); renew it yearly.
2. Next to `compose.yaml`, write `.env` (mode 600, it is in `.gitignore`):

   ```sh
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...   # from step 1, both containers get it
   SWARMIFY_PASSWORD=pick-a-long-one          # the operator's sign-in password, required
   SWARMIFY_BASE_URL=http://192.168.1.20:8000 # optional: where Slack deep links point
   SWARMIFY_BIND=0.0.0.0                      # optional: 127.0.0.1 keeps it off the LAN
   SWARMIFY_PORT=8000                         # optional: the host port
   BUILDX_NO_DEFAULT_ATTESTATIONS=1           # keeps an unchanged api image the same image
   ```

   With the containerd image store (Docker Desktop, Docker Engine 29 and later) every build
   otherwise attaches a fresh provenance attestation, which gives the api image a new id and
   makes `up -d --build` restart the api even when nothing in it changed.

   The api and graph-writer containers refuse to start without the token or the password, naming
   the missing one (`docker/needs.sh`).
3. `docker compose up -d --build`, then open `http://<host>:8000` from any device on the LAN,
   phone included, and sign in. `docker compose logs -f` shows every service; the api container
   is healthy once `/api/health` answers (open without signing in, like the skin's files). The
   first start installs the default packages into the volume (`/data/packages`), fetching their
   dependencies, so it needs the network once.
4. For narration, start the speech engines from the speech package's compose file beside it,
   with its GPU profile on an NVIDIA host:
   `docker compose -f connectors/speech/compose.yaml --profile chatterbox-gpu up -d --build`, and
   set the Speech engines Setting on the Connectors page to what that README says
   (`connectors/speech/README.md`, Running the engines beside Swarmify).

- Updating: `git pull`, then the same `docker compose up -d --build`, and the speech package's
  command with its profile. Only images whose inputs changed are rebuilt, and only their containers restart:
  a change under `apps/web` alone restarts `web` and leaves the api and its runs alone. For a
  UI-only update `docker compose up -d --build web` builds and restarts nothing else.
- Sign-in is enforced in one place, the api (`auth.py`). `web` passes `/api` and `/mcp`
  straight through and serves the skin's static files to anyone, so the sign-in page loads; no
  data comes back without a session or, on `/mcp`, an API token. While the api is down the
  skin loads and every call answers 502.

- Upgrading from V1, where `kokoro` and the `chatterbox` profiles were in this file: run
  `docker compose up -d --build --remove-orphans` once, which stops V1's engines (their ports
  would clash), then start them from the speech package's compose file as in step 4. A
  `SPEECH_ENDPOINTS` in `.env` (as `http://chatterbox-gpu:8881,http://kokoro:8880`) is carried
  into the package's Setting on that first start and keeps working, since the engines keep their
  service names on the shared network; the models volume is reused.
- Moving to Proxmox is a copy: the repo and `.env` onto the VM, `docker compose up -d --build`.
  To carry data across, stop both services and copy the volume, for example
  `docker run --rm -v swarmify_data:/data -v "$PWD":/out debian tar czf /out/data.tgz -C /data .`
  and the reverse `tar xzf` on the VM.
- Connector keys live in the volume's `/data/.env`, set from the connectors page as natively.
- A `remotion_build` stage sandboxes with bubblewrap (in the image, with `socat` for the Agent
  SDK's network filter). bubblewrap needs user namespaces and a fresh `/proc`, which Docker's
  default seccomp, AppArmor and masked paths refuse, so `compose.yaml` runs `api` with
  `security_opt: seccomp=unconfined, apparmor=unconfined, systempaths=unconfined`. That loosens the
  container to tighten the build inside it; without it a build stage fails rather than run
  unsandboxed.
- A Remotion template for the render connector must be inside the container: copy it to
  `/data/remotion` (`docker compose cp ./shorts api:/data/remotion/shorts`) and the stage's
  `project: shorts` finds it, as natively. The first render installs its dependencies. When
  `chrome-headless-shell` is on `PATH`, as in the image, the connector passes it to Remotion as
  `--browser-executable`; on the Mac Remotion uses its own.
- The image sets `SWARMIFY_RENDER_CONCURRENCY=2`, passed to Remotion as `--concurrency`: with no
  GPU a 3D scene (`@remotion/three`) renders in software, and the default of half the CPUs runs
  out of memory. Unset, as natively, Remotion picks. No `--gl` flag is passed: the default
  renders 3D on the Mac and in the image, and `--gl=angle` crashes in the image.
- Sign-in works natively too: `SWARMIFY_PASSWORD=... uv run uvicorn --factory
  swarmify_api.main:create_app --host 0.0.0.0` puts the Mac on the LAN behind the same sign-in
  page. With no password set or stored there is no sign-in, which is right only on `127.0.0.1`.
  For access from outside the house use Tailscale (see Sign-in) rather than opening the port.
- Docker Desktop on a Mac stuck at "load metadata" on every pull is its `desktop` credential helper
  hanging. Pulling public images needs no credentials, so point Docker at a config without it:
  `mkdir -p /tmp/dockercfg && ln -sf ~/.docker/cli-plugins /tmp/dockercfg/ &&
  echo '{"currentContext":"desktop-linux"}' > /tmp/dockercfg/config.json`, then prefix the
  commands with `DOCKER_CONFIG=/tmp/dockercfg`.

## Layout

- `packages/kernel` shared kernel: ids, the event envelope and its append-only log, SQLite
  access and the migration runner. The only package other packages may import.
- Migrations are numbered SQL files, `NNNN_name.sql`, applied in order and each recorded in the
  `migrations` table, so a lower number merged after a higher one still runs. Numbers are unique
  across the repo (take the next free one, renumber on rebase if another ticket took it): each package ships its own `migrations/` folder
  (kernel 0001, worlds 0002, 0019, 0020 and 0021, workflows 0003, runs 0004, connectors 0005, swarm 0006 and 0007,
  runs 0008, 0010, 0013 and 0017, swarm 0009, notifications 0016, learning 0022, the api's operator 0031, ...) and the composition root passes them all to `migrate` once.
  Adapters never migrate.
- `packages/<context>` one package per bounded context (worlds, workflows, runs, swarm, memory,
  connectors, learning, notifications). `packages/learning` declares its Storage port and `Ask`,
  the one question the Retro puts to the agent runtime, which the composition root answers with
  `Swarm.ask`. `packages/swarm` also declares AgentRuntime, with the
  Agent SDK adapter `SdkRuntime`; `packages/memory` writes notes and artifacts as files and declares GraphIndex, with
  `McpGraphIndex` and `FakeGraphIndex`, plus the GraphWriter. `tools/check_imports.py` fails CI if a context imports
  anything but the kernel and itself. Each context declares its ports in `ports.py` (ADR 0012);
  `packages/worlds`, `packages/workflows`, `packages/runs`, `packages/connectors`, `packages/swarm`,
  `packages/learning` and `packages/notifications` each have a Storage port with a SQLite adapter.
  Workflows learns which connectors a world allows through a function the composition root passes
  in, backed by the connector registry. `packages/connectors` also declares ConnectorTransport, with
  the MCP adapter and `FakeTransport` for tests.
- `apps/api` FastAPI composition root; serves `apps/web/dist` at `/` when it exists (natively
  and in the e2e suite; in Docker the `web` container serves it). Also home of
  the `swarmify-mcp` server, which exposes the same actions as MCP tools. Both delete a world
  through one `world_deleter`: its folder, schedules, live runs (cancelled through DBOS) and every
  context's rows.
- `apps/web` React skin with a typed client generated by openapi-typescript.
- `connectors/` the first-party Connector Packages, each with its Manifest, code, tests, README
  and any compose file for the Services it calls, outside the uv workspace; Swarmify installs
  them as default packages. `connectors/speech` (see Speech).
- `tests/e2e` Playwright for Python against the real API. `test_phone.py` opens every page at
  390 by 844 signed in and fails if any scrolls sideways.

## License

Swarmify is licensed under the GNU Affero General Public License 3.0 (`AGPL-3.0-only`, see
`LICENSE`, ADR 0022): use, change and self-host it freely, and if you run a modified Swarmify as a
network service, publish your changes under the same license.

A Connector Package is a separate program that talks to Swarmify over MCP, so it is a separate
work under its own license. Connector authors choose that license themselves and may publish
their packages under MIT or any other license.
