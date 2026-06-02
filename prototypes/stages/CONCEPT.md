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

- **Taps** — live ingress. Connected + incoming taps; each tap's **Input**
  kind, its **own** audio settings, and its Person mapping + single/multi.
- **People** — the canonical registry of **humans**.
- **Settings** — global engine/prompt/rule defaults.

### Vocabulary (Tap · Input · Person — used exactly this way everywhere)

- **Tap** — an incoming audio **stream** from a Bridge, keyed by `identity`
  (plus a `name`). A tap **owns** its audio settings — gate threshold,
  noise-floor, the speech-gate LiveConfig (`gate_kind` /
  `gate_speech_threshold` / `gate_hangover_ms` / `gate_pre_roll_ms` /
  `gate_min_speech_ms` / `confidence_validation`), and rec/live — and these
  persist **per identity** across sessions (matching the real backend's
  per-identity tap settings). A tap carries **one Person**, or **several**
  when diarization splits it.
- **Input** — the **kind** of audio a tap brings in: **microphone**,
  **line-in**, or **stereo-mix** (system / file / video audio). This replaces
  the old "device"/"microphone" framing of a tap. "Input" is a Tap property,
  never a Person attribute.
- **Person** — a canonical **human**. Holds **only** a name, a primary +
  secondary **language** (+ the "transcribe as EN/NB/DA" quick-switch), and the
  list of taps / diarized voices mapped to them. A Person has **no**
  gate/noise-floor/input profile — those are Tap settings.
- **Speaker = Person**: a tap's diarized voices ("Speaker A/B") are
  **not-yet-identified People**; each maps to a canonical Person or is left
  **"Unidentified — map to a Person."** A **room** or a **stereo-mix** is a
  **Tap**, never a Person — the humans heard through it are mapped from its
  diarized voices.

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

- A dense table of connected taps + one **incoming** (handshaking) tap. A row
  reads **name · identity · Input → mapped Person(s)**, plus a live **level
  meter** (0–1) + sparkline, **lag**, **gate** open/shut, and **rec/live**
  toggles. The **Input** column shows the tap's kind — *microphone*,
  *line-in*, or *stereo-mix*. The scenario shows variety: Atle's personal
  **microphone**, Mette's personal **microphone**, James on a **line-in**, the
  former Oslo Conference Room now a **stereo-mix** room tap carrying two
  People, and a played video/file as a second **stereo-mix** tap.
- Clicking a row expands the tap's **settings strip**: the in-flight buffer,
  the **Person** each voice maps to (→ People), the language (Person-owned),
  the **single ⇄ multi** switch, and — labelled as the tap's own settings,
  *"remembered per identity across sessions"* — its **gate threshold + noise
  floor** and the full **speech-gate LiveConfig** (`gate_kind`,
  `gate_speech_threshold`, `gate_hangover_ms`, `gate_pre_roll_ms`,
  `gate_min_speech_ms`, `confidence_validation`). For a **multi** tap, each
  diarized voice (Speaker A/B) maps to a Person; at least one is mapped to a
  named Person and at least one is left **Unidentified**.
- A global **Recording armed / paused** switch (`RECORDING_ENABLED`).
- *Net-new (mock):* multi-person/diarized taps (Speaker A/B), the per-tap
  gate/floor settings UI.

### GLOBAL · People (registry of humans)

- Canonical **People** — **humans only**. Each holds **only**: name, a
  **primary + secondary language** with a "transcribe as EN/NB/DA"
  quick-switch, and the **taps / diarized voices mapped** to them, plus "seen
  in N sessions". There is **no** gate/noise-floor/input profile here — those
  moved to the **Tap**. A "+ map a tap / voice" affordance ties more
  identities/voices to one Person.
- The former "Oslo Conference Room" is **gone** from People (it's a Tap). The
  two humans heard through it are real People: one (Henrik) is mapped from the
  room's diarized **Speaker A**, while the room's **Speaker B** stays in an
  **Unidentified voices** card — making the *Speaker = Person, not yet mapped*
  state explicit.
- A per-session **participation** strip (who's in the current session;
  multi-person taps expand to the People they carry).
- The real backend piece here is the per-session alias (identity → name);
  dual-language is *net-new (mock)*.

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

- Dense **IRC live captions** (`[m:ss] Person: text`, coloured per speaker,
  language flag, an in-flight cursor line). A room/clip voice that isn't mapped
  yet shows as **Unidentified (A/B)**, a mapped one as its Person.
- A **Live channel** control: state (running LED, start/stop switch), live
  **model** + **language** + backend, and a live-log peek.
- A read-only **"taps feeding this session"** reference (→ configure in Taps),
  showing each tap's **Input** kind and, for a multi tap, its voices mapping to
  People (mapped + Unidentified) — all still *visible*.
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

- A tight **IRC merged transcript**: each segment carries its **Person** (or
  **Unidentified (A/B)** for an unmapped diarized voice) + an identity badge,
  language, **confidence** (low-confidence dimmed + dashed), a **matched_rule**
  (suppressed hallucination struck through, audited, and **restorable**), and a
  **translation badge** (`nb→en`). A speaking-time bar sits on top;
  `models_used` / `backends_used` in the header; a collapsible filter audit
  folds in at the bottom.
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
