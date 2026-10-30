# youtube

The YouTube connector: it uploads a video to a YouTube channel through the YouTube Data API, now
or scheduled, and reads view counts back. It is our own small MCP server (ADR 0015) and a
Connector Package (ADR 0019), one of Swarmify's default packages: on startup Swarmify installs it
from this folder into `<data dir>/packages/youtube/<version>/`, where its install step (`uv sync`)
puts httpx and the MCP SDK beside it, and enables it for every world that has not decided
otherwise, so workflows call `youtube` exactly as before. Everything Swarmify knows of it is
`swarmify-connector.toml`.

## Setup

One field, **YouTube account** (`youtube`), a Google sign-in (Swarmify's README, OAuth sign-in): Swarmify
keeps the token set and starts the connector with a current access token as
`YOUTUBE_ACCESS_TOKEN`. It asks for the `youtube.upload` and `youtube.readonly` scopes, offline
access and the consent screen every time, so Google always issues a refresh token.

Once: create a Google Cloud project, enable the YouTube Data API v3, configure the consent screen
(while it is in Testing, add yourself as a test user) and create an OAuth client of type **Web
application** whose authorized redirect URI is the one the Connectors page shows,
`<the address you open Swarmify at>/api/setup/oauth/callback`. On the Connectors page save its client ID and secret as **YouTube account client ID** and
**client secret**, then **Sign in** with the Google account that owns the channel. The account is
shared by every world.

Upgrading from V1, where the terminal `authorize` command printed `YOUTUBE_REFRESH_TOKEN` for
`.env`: the start that first installs the package moves `YOUTUBE_CLIENT_ID` and
`YOUTUBE_CLIENT_SECRET` into the client ID and secret, and `YOUTUBE_REFRESH_TOKEN` into the
account's token set, refreshed on first use, so no new sign-in is needed. The `authorize` command
is gone. V1's client is a Desktop app, which Google lets redirect only to the machine itself: to
sign in again with it, use the loopback fallback (Swarmify's README, OAuth sign-in), or create a
Web application client as above. View counts need the `youtube.readonly` scope, which V1 did not
ask for: a V1 refresh token posts but reads no Outcomes until you sign in again.

Google and a `*.ts.net` address: Google accepts a redirect URI for a "Web application" client
when it uses https, its host is not a raw IP, and its top-level domain is on the public suffix
list ([Google's redirect URI rules](https://developers.google.com/identity/protocols/oauth2/web-server#uri-validation)).
`https://<host>.<tailnet>.ts.net/api/setup/oauth/callback` meets all three: `.net` is a public
suffix, and `ts.net` itself is on the [list](https://publicsuffix.org/list/public_suffix_list.dat)
(private section, submitted by Tailscale). Google never calls the redirect URI itself, the browser
follows it, so Tailscale Serve is enough. The Manifest asks Google for a refresh token with
`authorize_params = { access_type = "offline", prompt = "consent" }`.

A refresh Google refuses (the access revoked, the app's secret changed) or a call Google answers
401 marks the account **Needs sign-in**; sign in again from the Connectors page.

## The stage

```yaml
  - gate: post it
    kind: confirm
    ask: Post this short?
  - connector: publish
    calls: youtube          # or upload_post, whose README adds user and platforms
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
```

The `title` and `description` in the `metadata` file's frontmatter (and `hashtags` and `tags`)
win over the params, which stay the fallback, so a daily workflow posts each video under its own
script's title. `schedule` is ISO 8601 with a timezone, or relative: `09:00` (the next 09:00),
`+1d 09:00` (tomorrow at 09:00), either with an IANA timezone after it
(`+1d 09:00 Europe/London`, UTC by default). A relative schedule is resolved when the gate is
shown, so a scheduled workflow posts every run, and a gate answered after the time it showed
passed shows the new one again. `made_for_kids` goes to YouTube as `selfDeclaredMadeForKids`;
content made for children must set it. `hashtags` go out as the description's last line
(`#owls #nature`), where YouTube shows up to three above the title; a hashtag with a space or
punctuation, more than five, or a description over YouTube's 5,000 bytes with them is a problem
the gate shows. `tags` go to YouTube's tags (`snippet.tags`) beside the retry tag, which is why
they stop at 450 of YouTube's 500 characters. Both are editable at the gate. Writing them is the
workflow's job, for example the script stage's frontmatter; Swarmify never makes them up.

## What it answers

- `preview` shows Title, Description, Hashtags, Tags, Visibility, Made for kids, When and Video,
  and every problem that would stop the post: a missing or over-100-character title, an unknown
  visibility, a schedule that is unreadable, has no timezone, has passed or is not public, more
  than five hashtags or one that is not a single word, tags over 450 characters, a description
  over 5,000 bytes with its hashtags, no input. Params and the `metadata` file work as The stage
  says. A relative schedule (`09:00`, `+1d 09:00 Europe/London`) is resolved
  when the preview is made. It needs no account, so a saved workflow is checked before sign-in.
- `publish` posts the confirmed fields: the hashtags as the description's last line, the tags as
  YouTube's tags, `made_for_kids` as `selfDeclaredMadeForKids`. It uploads with `videos.insert`
  (one resumable session, one PUT of the file); with a schedule the video goes up private with
  `publishAt` and YouTube makes it public then. It answers `id`, `url` (`https://youtu.be/<id>`)
  and `status` (`published` or `scheduled`).
- `outcomes` (the Outcomes Capability, ranked by `views`) answers the view counts of every post
  that is a YouTube video: its own posts, and any post with a YouTube link, such as one
  Upload-Post made. Up to 50 videos per `videos.list` call.
- `channel` answers the title of the channel the account posts to.

Each upload is tagged `swarmify-<idempotency key>` beside its own tags, and before uploading the
connector looks for that tag among the channel's latest 50 uploads (3 quota units), so retrying a
failed publish stage answers the video an earlier attempt already put up instead of posting it
twice. By default uploads have their own quota of 100 a day; the retry check and the other reads
share 10,000 units a day
([quota costs](https://developers.google.com/youtube/v3/determine_quota_cost)).

## Tests

`test_youtube.py` covers the preview, the Data API calls against a fake Google (httpx's
MockTransport), and the package installed through the real install path from a V1 `.env`,
signed in by refreshing V1's token against a fake provider and answering `preview` and `publish`
over stdio MCP. Run it with `uv run pytest connectors/youtube` from the repository root.
