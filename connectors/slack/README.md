# slack

The Slack connector: Remote Approvals in Slack. Each gate a run opens is posted to the world's
channel with a button per answer that fits in one (Approve and Send back to the step before,
Confirm, or the first few picks), and a link to Swarmify for notes, edits and every other answer.
Once the gate closes, answered in Slack, in Swarmify or anywhere else, the message loses its
buttons and says who answered and how. A click on a gate that has moved on is refused, and only
the person who clicked sees why: the first answer wins.

It is a Connector Package (ADR 0019) of kind `notify`, offering the `remote_approvals`
Capability. Swarmify installs it from this folder with its other packages, its install step
(`uv sync`) putting Slack's Bolt and SDK and the MCP SDK beside it (ADR 0026), but no world is
given it: enable it on the Connectors page of each world whose gates should go to Slack. Until a
world has it enabled and set up, that world's Slack webhook (Settings, Notifications) keeps
telling you about gates, as in V1; after, gates come through this package and the webhook keeps
only the budget warnings and run endings.

## How it runs

Two processes, both started by Swarmify with the package's Setup values:

- `slack_connector.py`, the stdio MCP server Swarmify calls per gate: `post_gate` and
  `update_gate`, plus `check_setup`, the Setup test.
- `slack_connector.py listen`, the Manifest's `listen` command: a Bolt app in Socket Mode, an
  outbound websocket to Slack, so Swarmify needs no public URL. Swarmify keeps it running while a
  world enables the package with its Setup done (restarting it within 15 seconds if it exits) and
  gives it `SWARMIFY_MCP_URL` and `SWARMIFY_API_TOKEN`: a token Swarmify makes for it, which
  reaches only `/mcp/remote_approvals`, where a click answers its gate as `<name> in Slack`. The
  token is listed under Settings, API tokens, as "slack remote approvals"; it is replaced on each
  start of Swarmify.

`SWARMIFY_MCP_URL` is built from `SWARMIFY_BASE_URL`, so that address must reach the API from
where Swarmify runs (it does by default, `http://localhost:8000`).

## Setup

Make a Slack app of your own at <https://api.slack.com/apps> (From scratch), then:

1. **Socket Mode**: turn it on. Slack asks for an app-level token: give it the
   `connections:write` scope and keep the `xapp-` token.
2. **Interactivity & Shortcuts**: turn it on (Socket Mode needs no request URL).
3. **OAuth & Permissions**: add the bot scope `chat:write`, install the app to your workspace and
   keep the `xoxb-` bot token.
4. Invite the app to the channel for the gates (`/invite @your-app`) and copy the channel's ID
   from its details, as `C0123ABCDEF`.

Then on the Connectors page of each world: enable **Slack**, and in its Setup fill in
**Bot token** and **App-level token** (Credentials, shared by every world unless one sets its
own) and **Channel** (per world). Saving runs the Setup test, which signs in with both tokens.

## Tests

`test_slack.py` drives everything with a fake Slack client and never calls Slack: the message and
its update, and a click answering a gate through a live API on DBOS, with a web answer updating
the message too, after the package is installed through the real install path.
