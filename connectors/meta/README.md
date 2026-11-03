# meta

The Meta connector: it posts Reels to a world's Instagram account and the Facebook Page it is
linked to through Meta's Graph API (v26.0). It is our own small MCP server (ADR 0015) and a
Connector Package (ADR 0019), one of Swarmify's default packages: on startup Swarmify installs it
from this folder into `<data dir>/packages/meta/<version>/` and enables it for every world that
has not decided otherwise. Everything Swarmify knows of it is `swarmify-connector.toml`.

It posts Reels to Instagram and the Facebook Page, a retry never posts twice, and it reads
back their views as Outcomes.

## The stage

```yaml
  - gate: review
    ask: Watch it. Approve or send back with a note.
  - connector: post
    calls: meta
    input: short.mp4
    params:
      platforms: [instagram, facebook]   # the default; either one alone works too
      metadata: script.md   # its frontmatter's title, description and hashtags win
      share_to_feed: true   # the default: in the profile grid too, not only Reels
      cover_at: 2.5         # the second the cover shows
      trial: false          # true: a Trial Reel, shown to non-followers first
      visibility: public    # draft: a Facebook draft only the Page's admins see
      schedule: +1d 09:00   # Facebook only, 10 minutes to 29 days ahead
```

The caption is the description, or the title when there is none, then a blank line and the
hashtags as one `#a #b` line. The metadata file's frontmatter wins over the params; `tags` are
ignored. The gate shows the caption and hashtags to edit, and the Platforms, visibility, schedule,
Trial Reel, share to feed, cover and video as they will go. The connector never adds text of its
own.

The preview lists each problem with its Platform. `Instagram: ...`: a caption over 2,200
characters (hashtags included), more than 30 hashtags, a hashtag that is not one word, more than
20 @mentions, any `schedule` (Instagram cannot schedule; schedule the workflow instead) and
`visibility: draft` (Instagram has no drafts). `Facebook: ...`: a video over 90 seconds, a
schedule that has passed or is not 10 minutes to 29 days ahead, and a scheduled draft. With no
input, an unreadable schedule, or a param of the wrong kind, the stage cannot run either.

The core names the input to the preview by its file name, so the preview measures the video only
when it can read it. `publish` measures it again (from the MP4's `mvhd` header) before posting to
Facebook: a video over 90 seconds fails on Facebook and still posts to Instagram.

On Instagram, `publish` creates a Reels container (`upload_type=resumable`, with the caption,
`share_to_feed`, `thumb_offset` from `cover_at` and, for a Trial Reel, `trial_params` with
graduation `MANUAL`, so it stays a trial until you share it to followers in the app), uploads the
file itself to `rupload.facebook.com` (no public URL), checks the container's `status_code` every
10 seconds for up to 10 minutes until it is `FINISHED` (`ERROR` or `EXPIRED` fails with Meta's
reason), then publishes it with `media_publish`. It answers the post's media ID as `id` and its
permalink under `links.instagram`.

On the Facebook Page, `publish` starts the Page's Reels upload (`video_reels` with
`upload_phase=start`), uploads the file itself to `rupload.facebook.com`, then finishes it with
the caption as `description` and a `video_state` of `PUBLISHED`, `DRAFT` (`visibility: draft`)
or `SCHEDULED` (with `scheduled_publish_time` from `schedule`, and `status: scheduled`). Its link
is `https://www.facebook.com/reel/<video id>` under `links.facebook`.

One publish record holds each Platform's link under `links` and each failure under `failed`; `id`
is Instagram's media ID, or Facebook's video ID when Instagram did not post. The stage fails only
when no Platform posted.

Meta has no idempotency key, so a stage keeps what it posted in `meta-<key>.json` in its folder of
the run (`<world>/runs/<run>/<stage>/`), named after the stage's Idempotency Key: each Platform's
container or video ID once its file is uploaded (Facebook's from the start), then its post once
finished. A retry of the stage reads it and redoes only Platforms without a finished post, which
it answers as recorded. A recorded Instagram container is published (`FINISHED`), found among
the account's newest 25 posts by its caption when Meta published it but its answer was lost
(`PUBLISHED`), or made again after 24 hours (`EXPIRED`); its file is never uploaded again. A
recorded Facebook video is answered when its `publishing_phase` is complete, uploaded to again
when its upload never completed, then finished. A stage visited again gets a new key, so a new
record.
## Setup

Two fields per world, so each world posts as its own brand's Page and Instagram account, and one
shared:

- **Login configuration ID** (`meta_config_id`), shared by every world: the ID of the app's
  login configuration, in the Meta app under Facebook Login for Business, Configurations.
  Sign-in sends it as the authorize URL's `config_id` (`authorize_settings`, Swarmify's README,
  OAuth sign-in) and refuses to start without it.
- **Meta account** (`meta`), a Facebook sign-in (Swarmify's README, OAuth sign-in) that this
  package finishes (ADR 0029). After Swarmify exchanges the code, the package's `signed_in` tool
  swaps the token for a long-lived one, lists the account's Pages and keeps the token of the one
  Page with a linked Instagram account. A Page token made this way never expires, so it is never
  refreshed. The connector gets it as `META_ACCESS_TOKEN`.
- **Facebook Page** (`page`), optional: only when the account manages several Pages with a linked
  Instagram account, the name or ID of the one to post to, as `META_PAGE`. Sign in again after
  changing it. Without it, such a sign-in fails naming the Pages it found.

Once: create a Meta app of type Business with Facebook Login for Business, add a login
configuration for a user access token with `pages_show_list`, `pages_read_engagement`,
`pages_manage_posts`, `instagram_basic`, `instagram_content_publish` and `read_insights` (plus
`business_management` when the app belongs to a business portfolio), and add the redirect URI the
Connectors page shows, `<the address you open Swarmify at>/api/setup/oauth/callback`, to Valid
OAuth Redirect URIs. While the app is in Development mode, everyone with a role on it can grant
every permission, so the operator's own Pages need no App Review. Link the Instagram account
(Professional, Business or Creator) to the Facebook Page.

Then on the Connectors page save the configuration's ID as **Login configuration ID** and the
app's ID and secret as **Meta account client ID** and **client secret** (all shared by every
world), and **Sign in** in each world.

A token Meta refuses (error code 190, or an `OAuthException`) fails the call with `auth_failed`
and marks the account **Needs sign-in**; sign in again from the Connectors page.

## What it answers

- `signed_in` (called by the core after the code exchange, never by a stage) takes
  `access_token`, `client_id` and `client_secret` and answers `{"access_token": <the Page
  token>, "expires_at": null}`. It fails, naming the Pages, when none or several have a linked
  Instagram account, or when the `page` Setting names no Page or one without Instagram.
- `account` answers the Page the token is for and its Instagram account ID (`page`,
  `instagram`), read with `me?fields=id,instagram_business_account`, as each call will start.
- `outcomes` (the Outcomes Capability, ranked by `views`) answers, for every post with an
  Instagram or Facebook link (its own, or one Upload-Post made), `instagram_views` (the media
  insights metric `views`), `facebook_views` (the Reel's `video_insights` metric
  `fb_reels_total_plays`) and their sum `views`. A single-Platform post answers its Platform's
  metric and `views`. An Instagram link names a shortcode, not a media ID, so the account's latest
  1,000 media are listed to find it; a post that is not among them, or not the world's account's,
  is left out. Meta failing for one post leaves out only that post's Platform; a refused token
  fails the call with `auth_failed`.

A stage that posts only a Facebook draft (`platforms: [facebook]`, `visibility: draft`) posts
privately and needs no gate (`private_when`); every stage with Instagram is gated.

## Tests

`test_meta.py` covers `signed_in` and the account lookup against a fake Graph API (httpx's
MockTransport): the long-lived exchange, the Page kept with one or several Pages, the `page`
Setting, Pages without Instagram and `auth_failed`. Then the preview (the caption, metadata and
edits, every problem) and the Instagram publish in order (container, upload, wait, publish), its
params and a container that fails processing. Then the Facebook Page: the MP4 length, the Facebook
problems, start, upload and finish, draft and scheduled states, a long video that still posts to
Instagram and one Platform failing while the other posts. Then retries: the same container
published or found after Instagram's upload, the same video finished or answered after
Facebook's start, only Facebook redone after Instagram posted, and a new key never reading another
stage's record. Then `outcomes`: views per Platform and their sum, another connector's Instagram
link, posts left out and one post's error. Then the package installed through the
real install path, answering over stdio MCP in its sandbox, and a workflow with an Instagram stage
and no gate refused when saved. Run it with `uv run pytest connectors/meta` from the repository
root.
