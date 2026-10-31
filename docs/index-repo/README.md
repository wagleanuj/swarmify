# swarmify-connectors

The curated index of [Swarmify](https://github.com/wagleanuj/swarmify) Connector Packages. One
file, `index.toml`, lists the packages operators can browse on Swarmify's Connectors page: what
each does, where its git repository is, the ref to install, and the permissions its Manifest
declares. It is a list, not a marketplace: nothing is hosted here but the list, and installing
an entry is the same as installing its git URL by hand.

Swarmify reads this file from `SWARMIFY_CONNECTOR_INDEX`, by default
`https://raw.githubusercontent.com/wagleanuj/swarmify-connectors/main/index.toml`. Point that
setting at your own fork, over https, or at a local file to run a private index.

## The format

One `[[packages]]` table per package:

```toml
[[packages]]
name = "speech"                      # the `name` in the package's swarmify-connector.toml
description = "Voices a script, one take per scene."  # one or two plain sentences
git = "https://github.com/you/your-connector.git"     # public, https, no credentials in it
subfolder = "connectors/speech"      # optional: where the package is when not at the root
ref = "v1.0.0"                       # the recommended tag (or branch, or commit)
permissions = [                      # every permission its Manifest declares, as Swarmify's review lists them
  "network: api.example.org",
  "credentials: EXAMPLE_API_KEY",
]
```

- `name`: lowercase letters, digits and `_`, starting with a letter, matching the Manifest.
- `permissions`: one line per permission, `<kind>: <value>`. The kinds are `network`,
  `install_network`, `folders`, `services`, `owned_folders`, `credentials`, `sign_in` and
  `capabilities`. The simplest way to get them right is Swarmify's own review: on the
  Connectors page choose Install a package, enter your git URL and ref, and copy the list it
  shows (or `POST /api/packages/review` and copy `permissions`).
- Fields the format does not name are ignored, so a newer index still reads in an older
  Swarmify. A file that is not valid TOML, or an entry missing a field, makes Swarmify show the
  operator the first bad field instead of the list, so keep the file valid.
- The file stays under 1 MB.

The permissions here are a summary for browsing. They are never what an operator approves:
installing an entry clones it at its ref, reads its Manifest without running anything, and asks
the operator to approve the permissions that clone actually declares.

## Adding a package

Open a pull request that adds one `[[packages]]` table to `index.toml`, at the end, and nothing
else. In the description, link the package's repository and say in a sentence what it is for.
To update an entry (a new recommended ref, a changed permission), change only that table.

Before opening it:

1. The package installs from a clean Swarmify through Install a package, at the `ref` you list,
   and its review shows exactly the `permissions` you list.
2. The repository is public and has a license.
3. The README of the package says what it does, what it needs (Services, accounts, Credentials)
   and how to set it up.

## What reviewers check

- The entry parses and has every field, and `name` matches the Manifest at that ref.
- The `git` URL is a public https repository; `ref` is a tag (preferred) or a commit, not a
  moving branch, for anything but this project's own packages.
- The review of the clone at `ref`, run in Swarmify, lists exactly the entry's `permissions`.
- Every permission is one the description explains: each `network` host, folder, Service,
  Credential and Capability has a reason a reader can see. Broad grants (a wildcard host,
  `folders` outside the package's own) need a stated reason.
- The install step only installs dependencies from its runtime's registry and the hosts in
  `install_network`.
- The description is plain and accurate and makes no claim the package does not back.
- One package per pull request; no change to other entries.

Merging an entry lists a package; it does not vouch for it. Operators approve every package's
permissions themselves when they install it.

## The first-party packages

The packages Swarmify ships live in its own repository under `connectors/<name>/`, so their
entries point there with a `subfolder`, at the `v2` branch until V2 is released and tagged.
When their Manifest is stable they move into their own repositories under MIT and their entries
here follow.

## License

MIT, see `LICENSE`. Each listed package keeps its own license.
