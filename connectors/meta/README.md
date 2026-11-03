# meta

The Meta connector: it posts Reels to a world's Instagram account and the Facebook Page it is
linked to through Meta's Graph API (v26.0). It is our own small MCP server (ADR 0015) and a
Connector Package (ADR 0019), one of Swarmify's default packages: on startup Swarmify installs it
from this folder into `<data dir>/packages/meta/<version>/` and enables it for every world that
has not decided otherwise. Everything Swarmify knows of it is `swarmify-connector.toml`.

Publishing (`preview`, `publish`, `outcomes`) is not built yet: `preview` answers that as a
problem and `publish` fails.

## Setup

Two fields, both per world, so each world posts as its own brand's Page and Instagram account:

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

The login configuration ID is a static authorize parameter of the Manifest (`authorize_params =
{ config_id = ... }`), which no Setting can fill. Replace `YOUR_LOGIN_CONFIGURATION_ID` in
`swarmify-connector.toml` with your configuration's ID and bump the package's version, then on
the Connectors page save the app's ID and secret as **Meta account client ID** and **client
secret** (shared by every world) and **Sign in** in each world.

A token Meta refuses (error code 190, or an `OAuthException`) fails the call with `auth_failed`
and marks the account **Needs sign-in**; sign in again from the Connectors page.

## What it answers

- `signed_in` (called by the core after the code exchange, never by a stage) takes
  `access_token`, `client_id` and `client_secret` and answers `{"access_token": <the Page
  token>, "expires_at": null}`. It fails, naming the Pages, when none or several have a linked
  Instagram account, or when the `page` Setting names no Page or one without Instagram.
- `account` answers the Page the token is for and its Instagram account ID (`page`,
  `instagram`), read with `me?fields=id,instagram_business_account`, as each call will start.

A stage that posts only a Facebook draft (`platforms: [facebook]`, `visibility: draft`) posts
privately and needs no gate (`private_when`); every stage with Instagram is gated.

## Tests

`test_meta.py` covers `signed_in` and the account lookup against a fake Graph API (httpx's
MockTransport): the long-lived exchange, the Page kept with one or several Pages, the `page`
Setting, Pages without Instagram and `auth_failed`. Then the package installed through the real
install path, answering over stdio MCP in its sandbox. Run it with
`uv run pytest connectors/meta` from the repository root.
