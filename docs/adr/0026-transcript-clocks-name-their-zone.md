---
status: accepted
date: 2026-09-30
---

# Transcript clocks name their zone; on-screen panes stay bare

One merged transcript renders two ways. The server writes `plain_text`
(`session_merge.render_transcript_text`) into `session-transcript.txt` and into
the summarizer's stdin. The browser builds the clipboard copy and the `.txt`
download (`transcript.buildCopyText`, which the download reuses byte for byte).
Both showed a bare `HH:MM:SS`, and that is ambiguous the moment the text leaves
the screen it was read on: a model reads the transcript and quotes those times
into notes nobody reads beside a clock (#438).

The rule, stated once in each renderer's docstring and cross-named: **a
rendered transcript line's clock names the zone it is in; a line on screen
needs none.** Text artefacts carry `HH:MM:SS±HH:MM`, each naming its own zone.
The merged pane, the live feed and the recordings spans keep bare `HH:MM:SS`
in the viewer's zone, which is the context there. See
[Zoned clock](../../CONTEXT.md#zoned-clock).

Nothing is threaded, because each renderer already holds the instant's zone.
`_clock` reads `datetime.utcoffset()` and preserves whatever offset a stored
ISO carries, since `parse_iso` never converts and merge instants are UTC.
`formatters.fmtClockZ` resolves ICU's per-instant `longOffset` under a pinned
`en-US` locale, so the artefact bytes are stable ASCII in every viewer
locale, and DST is the instant's, not today's. A naive stamp is `+00:00`, the
`parse_iso` naive-is-UTC convention.
An unreadable stamp stays `??:??:??` (JS `?`): an unknown time cannot name a
zone.

## Consequences

**Old stored bodies stay bare until re-merged.** `batch_summarize._summary_input`
and `buildCopyText` fall back to the stored `plain_text`, so a transcript
merged before this change renders as stored. That is honest old data, not
wrong data, and re-merging the session rewrites the artefact zoned.

**`fmtSessionLabel` stays bare UTC.** It labels a session from the id's own
slice, not a transcript line, so the rule does not reach it. It is the
remaining bare UTC spelling in the UI.

## Considered options

**Local everywhere** (render each artefact in the viewer's zone). It needs a
viewer timezone threaded into the summarize request, and
`session-transcript.txt` is written by batch jobs that have no viewer. There
is no local for the server.

**UTC everywhere** (convert every clock). The pane would then disagree with
every other clock in the UI and with the wall clock the meeting happened on.

**A `times are UTC` header.** It evaporates the moment a line is excerpted or
quoted, which is exactly the flow that surfaced #438.
