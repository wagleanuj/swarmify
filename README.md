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

A connector stage calls its connector (see Connectors below); a gate waits for a decision that only
`Runner.answer_gate` can send (T05 adds the inbox and the HTTP and MCP actions), and swarm and
workflow stages fail the run with a clear error until the swarm (T07) and child runs (T14) ship.
`GET /api/worlds/<id>/runs` lists runs newest first; `GET /api/runs/<id>` returns one with its stages.

## Connectors

A connector is an MCP server (a command line or an http(s) URL), the credential names it needs,
its kind (tool, stage or publish) and the stage types allowed to use it (swarm, connector). A
publish connector is only ever allowed in connector stages, and the editor makes it follow a gate.
Every world starts with the built-ins: `web_search` and `graph` (swarm tools, no setup) plus
`remotion_render`, `youtube` and `upload_post`, declared so workflows can name them; their servers
ship with T11 and T12, and until then calling one fails the stage with a clear error.

Credentials live in one file, `<data dir>/.env` (`~/.swarmify/.env`), written with mode 600; the
app refuses to read it when group or others can. Values never enter the database, a run, a
workflow or an API response. A command connector gets exactly its own credentials as its process
environment, merged over the MCP SDK's safe defaults (HOME, PATH, USER, SHELL, TERM, LOGNAME); a
URL connector gets them as request headers of the same name.

- `GET /api/worlds/<id>/connectors` lists built-ins first, each with the credential names still
  `missing` from the file.
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

`docker compose up --build` builds the api image (web skin baked in, served at `/`) and a
graph writer stub.

## Layout

- `packages/kernel` shared kernel: ids, the event envelope and its append-only log, SQLite
  access and the migration runner. The only package other packages may import.
- Migrations are numbered SQL files, `NNNN_name.sql`, applied in order and recorded in
  `PRAGMA user_version`. Numbers are global: each package ships its own `migrations/` folder
  (kernel 0001, worlds 0002, workflows 0003, runs 0004, connectors 0005, ...) and the composition root passes them all to `migrate` once.
  Adapters never migrate.
- `packages/<context>` one package per bounded context (worlds, workflows, runs, swarm, memory,
  connectors, learning, notifications). `tools/check_imports.py` fails CI if a context imports
  anything but the kernel and itself. Each context declares its ports in `ports.py` (ADR 0012);
  `packages/worlds`, `packages/workflows`, `packages/runs` and `packages/connectors` each have a Storage port with a
  SQLite adapter.
  Workflows learns which connectors a world allows through a function the composition root passes
  in, backed by the connector registry. `packages/connectors` also declares ConnectorTransport, with
  the MCP adapter and `FakeTransport` for tests.
- `apps/api` FastAPI composition root; serves `apps/web/dist` at `/` when it exists. Also home of
  the `swarmify-mcp` server, which exposes the same actions as MCP tools.
- `apps/web` React skin with a typed client generated by openapi-typescript.
- `tests/e2e` Playwright for Python against the real API.
