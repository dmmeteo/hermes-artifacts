---
name: operating
description: Use when publishing, finding, downloading or sharing Hermes Artifacts with the artifact_* tools.
---

# Operating Hermes Artifacts

One shared, private library holds every artifact. No agent or profile owns it. `source_profile` records who published an artifact; it is a label and a filter, never a permission. Any agent configured for the same service sees the same library.

## Publish is private, Share is a separate decision

- `artifact_publish` saves a file (`path`) or inline text (`content` + `kind`). Formats: html, markdown, svg, png, webp. HTML must be self-contained; it runs sandboxed with no network. The result has `artifact.id`, `artifact.slug` and the private `dashboard_path`. Publishing creates no link.
- Call `artifact_share` only when the user asks for a link someone else can open. One call creates one link for one artifact, default 7 days (`expires_in_hours`, max 8760). The URL is returned once. Give it to the user as-is.
- Artifacts are immutable. To change one, publish a new artifact; existing links keep showing the old bytes. There is no edit or replace.

## Which address to give

- Private library address: `dashboard_path` (`/artifacts/<slug>`) in the Hermes dashboard, or `/console/<slug>` on the standalone console when the operator runs one. Both need the operator's sign-in. Use these when the user wants to see it themselves.
- Guest link: the `url` from `artifact_share` (`<public host>/<22-character token>`). Anyone holding it can view until it expires or is revoked; it can be forwarded.
- Never build a guest URL yourself from an id, slug or hash. Only `artifact_share` creates one.

## Find and read

- `artifact_list`: newest first, metadata only. Filters `query` (title), `kind`, `profile` (attribution). `limit` up to 100; pass `next_cursor` back as `cursor` until it is null.
- `artifact_get`: metadata, every share with status (`active`, `expired`, `revoked`) and a text preview for html, markdown and svg.
- `artifact_download`: writes the original bytes atomically to the resolved `path`. It follows Hermes's write policy and refuses destinations requiring approval. `overwrite` permits replacing an allowed existing file, not granting approval.

## Revoke and expiry

- `artifact_revoke_share` takes `share.id` (from `artifact_share` or `artifact_get`), not the URL. Other links to the same artifact keep working.
- Expired, revoked, unknown and malformed links all show the same "link unavailable" page. Revocation cannot recall copies already downloaded.

## Errors

- `not_configured`: `HERMES_ARTIFACTS_URL` and `HERMES_ARTIFACTS_TOKEN` are missing for this profile. Tell the user; do not guess values.
- `service_unreachable`: the artifact service is down or the URL is wrong.
- `unauthorized`: the configured token was rejected; the operator must fix it.
- `invalid_artifact`: wrong kind for the bytes, empty content or a bad title.
- `too_large`: the content exceeds the service limit (10 MiB default).
- `invalid_arguments`: the tool call is invalid, the resolved file path is refused by Hermes, or the filesystem operation failed. Publishing by path requires a supported extension and an explicit `kind` must match it. Choose an allowed path or fix the arguments.
- `not_found`: unknown id, slug or share id.

## Secrets

The service token comes from Hermes's active secret scope. Never print it, put it in a URL, write it to a file or pass it to a tool. Treat guest URLs as secrets too: share them only with the user who asked, and do not paste them into artifacts, logs or other channels.
