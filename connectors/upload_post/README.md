# upload_post

The Upload-Post connector: it posts or schedules a video to YouTube, TikTok and Instagram through
[Upload-Post](https://www.upload-post.com). It is our own small MCP server over Upload-Post's
HTTP API (ADR 0015), not Upload-Post's MCP, so it can answer Swarmify's publish contract. It is a
Connector Package (ADR 0019) and one of Swarmify's default packages: on startup Swarmify installs
it from this folder into `<data dir>/packages/upload_post/<version>/`, where its install step
(`uv sync`) puts httpx and the MCP SDK beside it, and enables it for every world that has not
decided otherwise, so workflows call `upload_post` exactly as before. Everything Swarmify knows
of it is `swarmify-connector.toml`.

## Setup

One Credential, **API key** (`api_key`, reaching the connector as `UPLOAD_POST_API_KEY`), from
app.upload-post.com under API keys, shared by every world. Saving it on the Connectors page runs
the package's Setup test, `check_key` (`GET /api/uploadposts/me`), which names the account; a
key Upload-Post refuses is not saved.

Upgrading from V1, where `upload_post` was a built-in running Upload-Post's MCP through `npx`:
the start that first installs the package copies `UPLOAD_POST_API_KEY` from the credentials file
(`<data dir>/.env`) into this shared Setup. Node and `npx` are no longer used for it.

Each post goes out as the profile its stage names (`params.user`); connect the platforms to that
profile at app.upload-post.com first.

## What it answers

- `preview` shows Title, Description, Hashtags, Tags, Platforms, Profile, Visibility, Made for
  kids, When and Video, with the same params, limits and problems as the `youtube` connector
  (its README, The stage) plus `user` (required, the Upload-Post profile) and `platforms`
  (default `[youtube]`, as `[youtube, tiktok]`). `made_for_kids` goes as Upload-Post's
  `selfDeclaredMadeForKids` field and `tags` as its `tags[]`. A schedule
  means nothing posts before then.
- `publish` sends the confirmed fields to `POST /api/upload` as a multipart form: `video`, `user`,
  `title`, `platform[]` per platform, `description` with the hashtags as its last line,
  `scheduled_date`, `async_upload=false`, and when posting to YouTube `tags[]`, `privacyStatus`
  and `selfDeclaredMadeForKids`. The stage's idempotency key goes as the `Idempotency-Key` header,
  so Upload-Post collapses a retry within a day into the first post. It answers each platform's
  link under `links` and each failure under `failed` (when nothing was posted the stage fails),
  or for a schedule `status: scheduled` and Upload-Post's job id; the links of a scheduled post
  are not read back yet.
- An upload over about 59 seconds moves to Upload-Post's background even when asked to wait, and
  answers a request id. `publish` then answers a long job (`job_id` and `poll: upload_status`),
  and the core polls `upload_status` every 2 s like any connector's job; it reads
  `GET /api/uploadposts/status` until the upload is no longer pending and answers `done` with the
  links and failures. A request Upload-Post no longer knows is a failure.
- `check_key`, the Setup test above.

Its tests (`test_upload_post.py`) run against a fake Upload-Post (httpx's MockTransport) and the
package installed through the real install path; nothing reaches the network.
