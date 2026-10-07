# Design decisions

Foundation version: 0.2 (house design-foundation). Status: reviewed against the running Hermes dashboard.

## Task and identity

The user is the Hermes operator. In the dashboard they browse what agents produced, read or use an artifact at full size, and occasionally hand one to a guest. A guest opens one link and sees one artifact.

The identity is Hermes itself, not a new brand. An existing product keeps its identity unless someone asks for a redesign (foundation authority order). So neither Typesafe nor Stripe.dev is the dominant reference here.

## Reference (decided)

**Dominant reference: the installed Hermes dashboard**, observed live in an isolated instance (Hermes Teal default theme; Nous Blue as the light theme). Kanban and Skills pages served as the in-product pattern. Reference screenshots: `evidence/spike/kanban-reference.png` and `evidence/spike/skills-reference.png` in the scratch folder.

Measured from the running dashboard: background `#041c1c` (Teal), card `color-mix(#ffe6cb 4%, #041c1c)`, border `color-mix(#ffe6cb 15%, transparent)`, radius `0.5rem`, display font "Collapse", mono "Courier Prime", uppercase tracked labels, and outlined or inverted pill-less buttons. Light theme background `#E8F2FD`.

What transfers:
1. Host tokens, panel-owned controls (iteration 3). The panel now also runs standalone, so it ships small `Button`, `Input`, `Select` (native) and `Badge` controls styled after the Hermes ones, driven by the same `--color-*` and `--radius` tokens. Inside Hermes the host tab sends the live theme's tokens to the panel, so every theme still reskins it; standalone, the defaults are Hermes Teal (dark) and Nous Blue (light) following `prefers-color-scheme`. The dashboard's web fonts are not available inside the iframe; the panel uses system fonts. The exception is layering: the SDK `Dialog`, `ConfirmDialog` and `Toast` portal to `<body>`, which the browser top layer hides in Full window and Fullscreen. Share and its revoke confirmation are therefore native modal `<dialog>` elements styled with the same tokens, and notices render inside the viewer.
2. The Kanban rhythm: a filter row with uppercase micro-labels above a bordered list, and quiet metadata in muted foreground.
3. Content first: the artifact gets the workspace. Chrome shrinks to one 44px toolbar.

Omitted: decorative motifs from the house references (dither, clouds, line art). They would fight Hermes' own identity and add nothing to a viewer.

## Standalone console

Same panel, same layout and states. A one-line masthead ("Hermes Artifacts" and a privacy note) replaces the dashboard's page header. The page is centred up to 1280px with a 16px minimum gutter.

## Product system

- Palette and themes: inherited entirely from the host. The page adds no colours. Artifact previews for Markdown and images get a light or dark reading palette that follows the host theme. Detection walks to the first opaque ancestor background and handles `color(srgb …)` values.
- Type: host fonts. Body is the host default; labels are 0.75rem uppercase, tracked 0.08em, to match Kanban.
- Layout: Library is filters plus a single-column list. Viewer is toolbar plus frame, sized to the viewport bottom (12px gap). It refits through a `ResizeObserver` on its ancestors when host banners appear.
- Modes: inline (fills the content column), Full window (the viewer joins the browser top layer as a manual popover, so it covers the sidebar, header and banners despite the host column's stacking context), Fullscreen (Fullscreen API on the trusted viewer, falls back to Full window). All three keep the same iframe in place, so artifact state survives. Share opens above whichever mode is active and closing it returns to that mode.
- Narrow (≤640px): one toolbar row. Back and Download become icon buttons with labels; Full window hides because Fullscreen falls back to it; metadata hides.
- Guest viewer: the artifact covers the whole viewport. The only chrome is a small translucent ellipsis at the bottom centre (44px touch target, 32×14px glyph, above the bottom safe area). It opens a compact menu above itself with the title, link expiry, a revocation note and Download. Escape, an outside click or tap, or tabbing away closes it. No Fullscreen: the artifact already fills the window. Its palette follows `prefers-color-scheme`.
- States: loading, empty (no artifacts, and no filter match), error (unreachable service, unconfigured plugin, with hints), share feedback inside the dialog, revoke confirmation, navigation-blocked notice.
- Motion: none beyond host transitions. The guest menu's short fade is disabled under reduced motion.

## Review evidence (REVIEW.md)

Screenshots live under `evidence/uat/screens/` in the scratch folder. Browser: Playwright Chromium. Viewports: 1440×900 and 390×844 (DPR 2). Themes: Hermes Teal (dark) and Nous Blue (light).

| Check | Result | Evidence |
|---|---|---|
| Identity matches host (tokens, components, rhythm) | pass | `desktop-dark-library.png`, `desktop-light-library.png` |
| Artifact fills available workspace | pass: frame 99.5–99.8% of the column width; 92% (1440px) and 90% (390px) of the height below the viewer top, the rest being the toolbar | `report.json` → `measurements.workspace_*` |
| Full window and Fullscreen keep state | pass | `desktop-dark-full-window.png`, `desktop-dark-fullscreen.png`, `desktop-dark-fullscreen-fallback.png` |
| Full window and Fullscreen are what the user sees and clicks (hit-tested at viewport grid points, 1440px and 390px) | pass | `desktop-dark-full-window.png`, `mobile-dark-fullscreen-fallback.png` |
| Share create, revoke confirmation and close work inline, in Full window and in native Fullscreen | pass | `desktop-dark-full-window-share-*.png`, `desktop-dark-fullscreen-share-*.png`, `mobile-dark-fullscreen-fallback-share-*.png` |
| Guest uses entire viewport | pass: frame equals the viewport at both sizes and in both schemes | `guest-*.png`, `measurements.guest_*` |
| Keyboard traversal and visible focus | pass: Tab reaches rows with a visible outline; Enter opens; Escape leaves Full window | `desktop-dark-keyboard-focus.png` |
| Empty, error and feedback states | pass | `desktop-dark-empty-filter.png`, `desktop-dark-service-unreachable.png`, `desktop-dark-share-created.png`, `guest-unavailable.png` |
| 390px layout, no horizontal overflow | pass | `mobile-dark-library.png`, `mobile-dark-viewer.png`, `mobile-light-viewer-webp.png` |
| Contrast | n-a for new colours (none added); host tokens apply |
| Reduced motion | pass by construction: no new motion except the guest menu fade, disabled under reduced motion |

Defects found and fixed during review: the light theme was misread as dark; the viewer overflowed the viewport when a host banner mounted late; the 390px toolbar wrapped and clipped "Fullscreen"; the share dialog had no padding and an oversized Revoke button. Acceptance later found two defects geometry checks had missed: the host sidebar covered Full window, and Share was invisible in native Fullscreen. Both are fixed and now hit-tested.

Remaining: on narrow screens, host banners above the page take about 250px. That is Hermes chrome, outside this plugin's scope.
