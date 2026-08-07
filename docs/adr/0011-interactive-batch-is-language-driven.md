---
status: accepted (extends ADR-0010)
---

# Interactive batch transcription is language-driven: the Transcript page drops the model picker

## Decision

The Transcript page has **no model picker** — it carries a
**candidate-language control** editing the same per-meeting
`session-meta.languages` field the Capture view writes and
`transcribe_session` reads. The **generalist** stays the global
`config/batch-model.txt`, chosen in Settings, never per-transcribe. **Both**
transcribe actions on the page — the session-range button and the per-WAV
re-transcribe — run the cover (generalist + any specialists for the meeting's
languages): exactly **one** routing behaviour.

This completes ADR-0010's "declare languages, not a model" principle for the
interactive batch path. The old picker made its choice the cover's
*generalist* while the candidate set stayed invisible — picking Parakeet
silently pulled in the `nb-whisper-large` specialist (on the faster-whisper
backend) via the default `{da, no, en}` set, a sidecar the operator never
asked for.

## Considered and rejected

- **Model picker alongside a language control** — keeps the "pick a model"
  framing and the which-control-governs confusion that caused the surprise.
- **Generalist-only single-WAV re-transcribe** — the two buttons would route
  differently, re-introducing the ambiguity.
- **A meeting-vs-per-person language mode toggle** — per-speaker pins are
  deferred to compose with diarization (#78); language is a per-meeting
  property, never a persistent property of a Person.
- **One-shot per-transcribe language choice** — forks the data model
  (`session-meta` is already what `transcribe_session` reads) and loses the
  per-meeting declaration.

## Consequences

- **Extends ADR-0010's scope**: the manual single-WAV path
  (`/api/transcribe`), out of scope there, runs the cover as a one-WAV slice.
  `transcribe_one`'s single-model shortcut is retired; its silent-WAV RMS
  pre-check (fast feedback on noise files) is kept.
- **The page shows what will actually run**: the selector edits the *override*
  (blank = inheriting the global default); a read-only readout shows the
  **effective** candidate set (marked "inherited") and the exact models —
  generalist + named specialists — so the specialist is visible *before*
  transcribing (the direct fix for the surprise). Requires serving the
  generalist and the specialist map to the frontend.
- **Save-on-transcribe (WYSIWYG)**: the transcribe buttons persist the current
  selection to `session-meta` before running — a stale set can never run; an
  explicit Save sets languages without transcribing. Inheritance is preserved
  until the operator changes the selection and transcribes.
- **Per-run model A/B from the UI is gone** — A/B a generalist by pointing
  `batch-model.txt` at it, from Settings (the ADR-0010 seam).
