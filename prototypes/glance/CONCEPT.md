# Glance → Focus

A calm status **home** of a few compact, glanceable cards (Live, Sessions,
Speakers, Engine, Tuning). Each card shows only a tiny *digest* — counts,
status dots, a sparkline, a "needs attention" hint — never full data. The home
answers **"what needs my attention right now?"** in one unhurried glance.
Selecting a card **zooms it into a single, dense, focused workspace** for that
concern; the other cards recede into a slim peripheral rail. Exactly one
workspace is in focus at a time, and there is always one obvious way back to the
glance home. **Density lives inside the focus; the home stays sparse.** The
firehose is structurally impossible — full data only ever appears *after* you
have chosen a focus.

## Why this stays "not too much at once / logically grouped"

- **One focus at a time.** The home is ~5 digest cards, nothing more. A focus
  view shows exactly one concern's full data. You never see every feature on one
  surface — the thing earlier prototypes got wrong.
- **The glance never disappears.** When zoomed in, the other concerns collapse
  to a 56px rail of status dots on the left. So you always have *one focus plus
  its peripheral context* — a control room, not a kiosk. The rail doubles as the
  way back (click the active concern's home pip, or the "← Glance" button).
- **Grouping by concern, not by widget.** Each of the 9 features lives in
  exactly one concern, with obvious separators:
  - **Live** → live taps (level/lag/gate/rec-live) + live captions tagged by
    speaker & language. Diarization shows up here as a *property of the room
    tap* (a tap that fans out into Speaker A/B), never its own screen.
  - **Sessions** → session list + a detail pane with the dense, line-oriented
    merged transcript (speaking-time, low-confidence, suppressed/audit,
    translation badges) and the per-WAV/clip listing.
  - **Clips / Tuning** → the representative WAV waveform with strip-silence cut
    points that re-cut live as you drag the three knobs (marquee).
  - **Speakers** → people with cross-session per-mic profiles + primary/secondary
    language and the quick "transcribe as EN/NB/DA" switch.
  - **Engine** → backend chips (cuda disabled) + model-by-family picker + Canary
    source→target selects.
- **Dense ≠ overwhelming.** Inside a focus we pack information tightly (compact
  rows, inline meters, no wasted whitespace) because the user has *opted into*
  that one concern. Between concerns we stay calm.

## Variations considered

1. **Mission-control tiles** — a 2×3 grid of equal cards; clicking one animates
   it to fill the viewport while siblings fade. Clean, but an equal grid reads as
   a "typical dashboard," and once zoomed the context vanishes entirely.
2. **Status ribbon + focus stage** *(BUILT)* — a persistent slim rail of
   glanceable mini-cards on the left, a large focus stage on the right. The
   selected concern fills the stage; the rest stay visible as peripheral status.
   Best embodies "one focus + its grouped context" and gives the cleanest way
   back. The home state is the same rail expanded into full-width digest cards,
   so glance↔focus is a single continuous motion, not two different screens.
3. **Briefing → drill** — one centered column of stacked summary rows, each an
   in-place accordion. Calm, but stacking reads as a list and in-place expansion
   fights the "fill the screen, others recede" feel.

Built #2. The home and the focus are the *same* five concerns at two zoom
levels: glance = all five as wide digest cards; focus = one expanded, four
collapsed to a status rail.
