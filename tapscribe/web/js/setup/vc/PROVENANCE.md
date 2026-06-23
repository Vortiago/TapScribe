# Vendored vanilla-components

Copied **verbatim** from the `vanilla-components` skill
(`~/.claude/skills/vanilla-components/`) for the standalone `/setup` page, which
is served before the dashboard's machinery is guaranteed loadable and so can't
share the dashboard's `templates.js` / BEM stylesheet.

Distribution is copy-verbatim (the vanilla-web way): **do not hand-edit** files
under this directory — re-copy from the skill to update. Layout:

- `lib/` — `templates.js`, `component.js`, `tone.js` (the vanilla-web engine)
- `components/<name>/` — the components the setup page composes
  (panel, button, field, chip, alert, spinner)
- `tokens.css`, `tones.css` — the design tokens + tone mixin

The page's own layout lives in `../setup.css` (app-level, unlayered so it can
override component structure); the dark-only theme is `color-scheme: dark` at the
root, which resolves every token's `light-dark()` to its dark side.

## Local deviation from upstream

`lib/templates.js` carries ONE local change: the `wireTheme` rotation uses
`?? "auto"` to satisfy this app's `noUncheckedIndexedAccess` tsconfig (stricter
than the skill's). It's behaviour-preserving (the modulo index is always valid)
and marked with a comment at the line. If you re-copy from the skill, re-apply
it — or better, push the null-safety upstream so the shim is unneeded.
