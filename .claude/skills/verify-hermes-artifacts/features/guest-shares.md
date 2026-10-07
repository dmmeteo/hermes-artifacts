# Guest shares

Promise: publishing never shares. A share is explicit, expires (default 7 days) and is revocable; its URL is `<ARTIFACTS_PUBLIC_URL>/<22 base64url characters>`. Expired, revoked, unknown and malformed links show one 404 page. The guest viewer fills the window with a bottom-centre ellipsis menu.

Code: `app/main.py` (public routes), `app/store.py` (`create_share`, `resolve_share`), `app/viewer/`.

Drive: `POST /console/api/artifacts/<id>/shares` with `{"expires_in_seconds": 600}` (or the Share dialog), `GET` the URL (200), `DELETE /console/api/shares/<share id>`, `GET` again (404). In a browser, `#menu-toggle` opens the menu.
