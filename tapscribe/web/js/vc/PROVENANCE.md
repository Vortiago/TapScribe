# Vendored vanilla-components

Copied **verbatim** from the `vanilla-components` skill (the Verktoykasse
toolkit) for the standalone `/setup` page, which is served before the
dashboard's machinery is guaranteed loadable and so can't share the dashboard's
`templates.js` / BEM stylesheet.

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
