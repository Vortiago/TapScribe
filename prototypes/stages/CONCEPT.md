# Stages — an operator-grade control surface

## The lens

A TapScribe session is not a flat bag of features — it is audio that **moves
through processing states**: it arrives on a tap and is captured live; those
recordings are split (strip-silence) and transcribed into one merged
transcript. Some things outlive any single session (the canonical People
registry, the global Settings defaults), and the live **Taps** ingress is
"always there" regardless of which session is open.

So the dashboard is a slim left **spine** in **two groups**, with one dense,
single-focus workspace on the right. You work IN one view at a time; the spine
is always visible so you know where the session is and what each stage needs.

## Two-group spine

**GLOBAL** — pinned at the top, **un-numbered**, set apart by a group divider.
These are *not* part of the session journey; they persist across sessions:

- **Taps** — live ingress. Connected + incoming taps, the global recording
  arm, the speech-gate config, and per-tap Person mapping + single/multi.
- **People** — the canonical Persons registry.
- **Settings** — global engine/prompt/rule defaults.

**THIS SESSION** — the numbered journey, with the **session picker** and a
**New session** button living in this group. It reads top-to-bottom as the real
pipeline and carries a progress fill:

1. **Capture** → 2. **Recordings** → 3. **Transcript**

`window.gotoView` accepts all six: `taps`, `people`, `settings`, `capture`,
`recordings`, `transcript`.

## Aesthetic: less bubbly, dense, utilitarian

This rebuild deliberately strips the earlier "consumer app" softness. It is a
**pro tool / terminal**, not a card-and-pill UI:

- **Minimal radius (≤3px), no pills.** Every chip, tag, switch and button is a
  hard-cornered rectangle. The one radius variable is `--r: 3px`.
- **Hairline separators + thin borders** instead of big rounded cards. The
  workhorse is a reusable 1px-ruled `.tbl` table; panels are flat-topped with a
  single header rule.
- **Tighter padding, denser rows.** Base font 12.5px, packed line-height.
- **Monospace for all data/numbers** (identities, levels, lag, dB/ms knob
  values, timecodes, model ids); system-ui only for prose.
- Dark layered slate with **one warm amber accent** doing the "where am I /
  what's live" work, plus per-speaker palette slots for identity.

Still **one clear focus per view**, logically grouped — dense within, calm
between. Never a firehose.

## Where every real feature lives

The brief: give *every* real TapScribe feature a home, and mock the net-new
ones (flagged inline as `mock UI`).

### GLOBAL · Taps (live ingress)

- A dense table of connected taps + one **incoming** (handshaking) tap. Per
  tap: device/mic label, live **level meter** (0–1) + sparkline, **lag**,
  **gate** open/shut, **rec/live** toggles. Clicking a row expands a config
  strip: the in-flight buffer text, the **Person** it maps to (→ People), the
  language (Person-owned), and the **single ⇄ multi** switch. A **multi** tap
  (the Oslo Conference Room) diarizes into **Speaker A (nb 58%) / Speaker B
  (en 42%)** shown inline.
- A global **Recording armed / paused** switch (`RECORDING_ENABLED`).
- A **Speech gate · LiveConfig** section with the real knobs that gate every
  tap: `gate_kind` (tapscribe | backend — *backend greyed/unsupported*),
  `gate_speech_threshold`, `gate_hangover_ms`, `gate_pre_roll_ms`,
  `gate_min_speech_ms`, and a `confidence_validation` toggle. No language here
  (that's Person-owned now).
- *Net-new (mock):* multi-person/diarized taps (Speaker A/B).

### GLOBAL · People (registry)

- Canonical **Persons**. Each: name, **primary + secondary language** with a
  "transcribe as EN/NB/DA" quick-switch, **multiple per-microphone profiles**
  (each mic carries its own gate threshold + noise floor), the **taps /
  identities mapped** to the Person, and "seen in N sessions". A "+ map another
  tap / mic" affordance ties more identities to one Person.
- A per-session **participation** strip (who's in the current session).
- The real backend piece here is the per-session alias (identity → name); the
  per-mic profiles and dual-language are *net-new (mock)*.

### GLOBAL · Settings (defaults)

- **Default engine** (visible): backend chips (auto/mlx/cuda/cpu — *cuda
  disabled*), a **model picker grouped by family** (whisper, nb-whisper,
  voxtral, parakeet, canary), each family tagged **batch only** vs **live +
  batch**, and **Canary translation** `source_lang` → `target_lang`.
- **Global prompt**, a separate **live-prompt**, and **hotwords** (textareas —
  the real `/api/config/{key}` files).
- A **Hallucination rules** list in the real format, tagged by kind: plain
  **substring**, `exact:` whole-line, `re:` **regex**.

### SESSION · 1 Capture (live)

- Dense **IRC live captions** (`[m:ss] Speaker: text`, speaker-coloured,
  language flag, an in-flight cursor line).
- A **Live channel** control: state (running LED, start/stop switch), live
  **model** + **language** + backend, and a live-log peek.
- A read-only **"taps feeding this session"** reference (→ configure in Taps),
  with the room's diarized A/B attribution still *visible*.
- **Capture health** (gates open x/y, recording x/y, max lag, languages).
- **Session overrides**: a per-session recording toggle and prompt/hotwords
  override that falls back to the global Settings values.

### SESSION · 2 Recordings (WIDE — file splitting, pre-transcript)

This is the dedicated home for strip-silence, given the full width:

- A **WIDE hero waveform** with strip-silence cut markers and **live re-cut**
  as the knobs drag (`computeRegions`): `min_silence_ms`, `pad_ms`,
  `speech_floor_db`. Real result stats update live — **clips**,
  **speech_seconds**, **in_seconds**, **kept %** — with strip / clear actions.
- A per-WAV list (originals + the **stripped region clips**) and an
  **original ⇄ stripped** source toggle.
- **Transcribe** actions: one WAV, or a **session range** (from / to + a
  **force** checkbox), with **job progress** (current/total — one job per
  session).
- A **per-WAV transcript cache**: a WAV can hold several `(backend, model)`
  transcripts; the table shows them and lets you pick the **primary**.
- *Net-new (mock):* the waveform cut preview.

### SESSION · 3 Transcript (merged result)

- A tight **IRC merged transcript**: each segment carries speaker (+ identity
  badge), language, **confidence** (low-confidence dimmed + dashed), a
  **matched_rule** (suppressed hallucination struck through, audited, and
  **restorable**), and a **translation badge** (`nb→en`). A speaking-time bar
  sits on top; `models_used` / `backends_used` in the header; a collapsible
  filter audit folds in at the bottom.
- A **visible engine selector** that is the **per-session override** of the
  Settings default (backend chips + model-by-family + Canary source/target) —
  shown as a real side panel, **not** a popover.

### Session management

The session picker surfaces **New session**, switch session, and the
**absorb / prune-empty / delete** session actions; New session drops into a
fresh, empty journey ("no taps yet / no WAVs / nothing yet") while the GLOBAL
group stays pinned and live.

## Why this shape (and not plain tabs)

Plain tabs are an unordered, stateless set of equal peers. Stages splits the
two real concerns — **global** config/ingress vs the **per-session** pipeline —
and orders the latter as the journey audio actually takes. Each stop is
stateful (a live chip + a progress fill, so the spine doubles as a readout), and
empty/late stages read as "not reached yet," which a tab bar can't express.

## Variations considered

- **(A) Two-group vertical spine + single workspace (CHOSEN).** Cleanly
  separates global from per-session, reads as a pipeline, keeps the rail narrow
  so the workspace is wide and dense (essential for the Recordings waveform).
- **(B) Horizontal stepper / wizard ribbon.** Competes with the in-stage header
  for the same band and can't express the global-vs-session split. Rejected.
- **(C) Card-deck / kanban.** The everything-at-once wall we're told to avoid.
  Rejected.

## Theme

Dark, flat "control surface": deep slate surfaces, hairline rules, one warm
amber accent for the active view + the live pulse, per-speaker palette slots for
identity. Monospace for data; system-ui for prose. An instrument panel, not a
consumer app.
