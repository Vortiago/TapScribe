# Lab — "The Signal Rack"

A clean-sheet, show-and-throw TapScribe prototype. The brief: think
outside the box about how to *structure* the features. The only taste
constraint: **dense UI with logical separations.**

## Three structurally different ideas I considered

1. **The Signal Rack (CHOSEN)** — TapScribe is literally an audio
   pipeline, so make the UI *be* the pipeline. The screen is a horizontal
   signal chain read left-to-right through named rails:
   `INPUTS → GATE → DIARIZE → IDENTITY → ENGINE → TRANSCRIPT`. Each rail is
   one stage; a tap is a "patch cord" that flows across all six. Diarization
   is a *fork on a tap's cord* (not a separate screen). Selecting any node
   docks a dense Inspector. A dedicated "Cut Lab" bench handles the
   waveform/strip-silence work.

2. **Mission-control event ledger** — a vertical time-ruler; everything
   (tap-open, caption-settle, clip-cut, hallucination-suppressed) is a
   timestamped event streaming past a "now" line. Dense, but it buries the
   *structural* relationships (which speaker owns which mic) in chronology.

3. **Queryable record grid (Airtable-as-OS)** — every entity is a row in a
   spreadsheet; you pivot Taps↔Speakers↔Sessions↔Clips through linked-record
   columns. Very dense and very "logical separations," but it's cold: it
   throws away the one thing that makes TapScribe legible — that audio
   *flows*. A grid can't animate a gate opening.

## Why the Signal Rack wins, and why its separations are logical

The feature set is not a bag of unrelated panels — it is a **directed
pipeline**, and every entity the brief lists is a *stage* of that pipeline:

```
 BRIDGE ─▶ TAP ─▶ GATE ─▶ [DIARIZE fork] ─▶ SPEAKER+LANG ─▶ ENGINE ─▶ TRANSCRIPT
 (source) (level/ (open/   (1 cord ->        (per-mic        (family/  (lines,
          lag)    closed)  A/B speakers)     profile)        backend)  audit)
```

So the rails ARE the logical separations — they're not invented categories,
they're the actual order the audio moves through the system. Reading the
screen left-to-right is reading a signal's life from microphone to text.
That gives us a "dense + separated" layout for free: six tall lanes, each
owning exactly one concern, with the live patch-cords tying them together.

### The four rooms (window.gotoView names)

| room        | what it is                                                    |
|-------------|---------------------------------------------------------------|
| `rack`      | the live signal rack — all 6 rails, every active tap as a cord, animated levels/gates. Feature 1,2,6 live here; the rails make 4 (engine) and 8 (lang) visible at the chain level. |
| `cutlab`    | the **Cut Lab** bench — hand-drawn waveform + strip-silence cut preview that **re-cuts live** as you drag the three knobs; the per-WAV/clip listing. THE marquee feature (5) + (9b). |
| `identity`  | the **Identity Bank** — cross-session per-mic speaker profiles (7), primary+secondary language + quick switch (8), and the diarization fork detail (6). |
| `transcript`| the **Transcript Ledger** — dense line-oriented merged transcript with speaking-time, low-confidence, suppressed-hallucination audit, translation (9). |

The Inspector dock (right edge) is shared chrome: clicking any node in any
room loads its dense readout there, so the rails stay scannable while detail
lives in one predictable place. The Engine rail header carries the
backend chips (cuda disabled), the model-by-family picker, and — only for
the Canary family — the source/target language selects (4).

### Density tactics

- 11–12px type, ui-monospace for all numbers/IDs, tabular alignment.
- Hairline `1px` rail dividers; color is reserved for *state* (gate open =
  green pip, suppressed = red strike, low-conf = amber) and *speaker
  identity* (a fixed 5-slot palette), never decoration.
- Every chart (level meters, sparklines, waveform, cut bars, talk-time
  bars, level history) is hand-drawn in `<canvas>`/`<svg>` — no libraries.

Color scheme: dark "studio rack" — near-black panels, phosphor-green
accents for live signal, per-speaker hues for identity.
