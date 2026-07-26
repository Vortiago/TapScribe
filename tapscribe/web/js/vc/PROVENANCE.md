# Vendored vanilla-components

Copied **verbatim** from the `vanilla-components` skill (the Verktoykasse
toolkit). It arrived for the standalone `/setup` page — served before the
dashboard's machinery is guaranteed loadable, so it can't reach for the
dashboard's seam — but the dashboard composes these components too (#306), and
the copy stays whole for a different reason: `vanilla-components` ships its own
`lib/`, and its components import `../../lib/templates.js` relative to
themselves. Re-pointing them at `web/js/lib/` would mean editing vendored files,
which is the one thing this layout forbids.

The visible cost is that `lib/templates.js` exists twice in this repo — here and
at `web/js/lib/templates.js` (the vanilla-web copy the dashboard seam builds
on). The two are byte-identical apart from their stamped revision, so the drift
gate reports the older one as `stale`, which is accurate and harmless. Do not
"fix" it by deleting one and re-pointing imports; the fix belongs upstream, in
how the toolkit distributes a shared `lib/` to an app that vendors both skills.

Distribution is copy-verbatim (the vanilla-web way): **do not hand-edit** files
under this directory — re-copy from the skill to update, never fork. Every file
carries a provenance stamp on its first line
(`from vanilla-components/<path>@<rev>` or
`canonical source: vanilla-web/<path>@<rev>`); the drift gate
(`node tapscribe/web/tools/check-vendored.mjs <toolkit-checkout>`, run from the
repo root) classifies each stamped copy as up-to-date / stale / forked and
prints the exact re-copy command for stale ones. Layout:

- `lib/` — `templates.js`, `component.js`, `tone.js`, `element.js`
  (the vanilla-web engine; `element.js` backs the optional `<vc-*>`
  element faces the component sidecars ship)
- `components/<name>/` — the components the setup page and the dashboard
  compose (panel, button, field, chip, alert, spinner, plus the
  dashboard-only progress and empty-state added with the #306 alignment)

The design tokens + tone mixin (`tokens.css`, `tones.css`) live at
`tapscribe/web/` — ONE vendored copy shared by this page and the dashboard
(both served as top-level `/tokens.css` + `/tones.css`; the dashboard's
`dashboard.css` overrides the values to TapScribe's palette).

To update: from the toolkit checkout's `vanilla-components/` run
`./vendor.sh <component> <this dir>/components` (and
`./vendor.sh tokens|tones tapscribe/web`); lib files are `cp` + re-stamp (see
`lib-stamp.sh` there).

The page's own layout lives in `../setup.css` (app-level, unlayered so it can
override component structure); the dark-only theme is `color-scheme: dark` at the
root, which resolves every token's `light-dark()` to its dark side.

There are no local deviations. (The one historical deviation — `?? "auto"` in
`wireTheme` for this app's stricter `noUncheckedIndexedAccess` — was upstreamed
in Verktoykasse #70.)
