---
status: accepted (extends ADR-0010)
---

# Interactive batch transcription is language-driven: the Transcript page drops the model picker

## Decision

The Transcript page previously exposed a **model/engine picker** whose choice
became the cover's *generalist*, while the [candidate-language
set](../../CONTEXT.md#candidate-languages--language-pin) stayed invisible — so
picking Parakeet silently pulled in the Norwegian `nb-whisper-large` *specialist*
(which loads on the **faster-whisper** backend) via the default `{da, no, en}`
set, surprising the operator with a faster-whisper sidecar they never asked for.

We **remove the model picker from the Transcript page entirely** and give it a
**candidate-language control** that declares the meeting's expected languages —
the same per-meeting `session-meta.languages` field the Capture view already
writes and `transcribe_session` already reads. This completes ADR-0010's
"declare languages, not a model" principle for the *interactive* batch path (the
one place a model-pick survived). The **generalist** stays the global
`config/batch-model.txt`, chosen in Settings, never per-transcribe. **Both**
transcribe actions on the page — the session-range button and the per-WAV
re-transcribe — run the cover (generalist + any specialists for the meeting's
languages), so there is exactly **one** routing behaviour on the page.

## Considered and rejected

- **Keep the model picker alongside a new language control.** Preserves the
  "pick a model" framing ADR-0010 warned against, and keeps the two-controls
  "which one governs what" confusion that caused the surprise in the first place.
- **Single-WAV re-transcribe uses the generalist only (no specialists).** The
  two buttons would then behave differently — one runs `nb-whisper`, one doesn't
  — re-introducing the ambiguity. Re-transcribing one WAV runs the same cover as
  the range, treated as a one-WAV slice.
- **A "meeting language vs per-person language" mode toggle.** ADR-0010 already
  deferred per-speaker pins to compose with diarization (#78), and the glossary
  holds that language is a per-meeting property, *never* a persistent property of
  a Person. A per-meeting control is the correct foundation for that later work,
  not a speculative toggle now.
- **One-shot per-transcribe language choice.** `transcribe_session` already reads
  candidate languages from `session-meta`; a one-shot would fork the data model,
  add `candidate_languages` plumbing to `BatchSessionRequest`, and lose the
  per-meeting declaration the ADR is built on.

## Consequences

- **Extends ADR-0010's scope.** The manual single-WAV path (`/api/transcribe`),
  explicitly *out of scope* in ADR-0010 because it stayed a model-pick, now runs
  the cover as a one-WAV slice. `transcribe_one`'s single-model shortcut is
  retired; its silent-WAV RMS pre-check (fast feedback on noise files) is kept.
- **The page must show what will actually run.** The selector edits the *override*
  (blank = inheriting the global default), so a read-only readout shows the
  **effective** candidate set (marked "inherited" when from the global default)
  **and the exact models** that will run — generalist + named specialists (e.g.
  "`whisper-large-v3-turbo` (generalist) + `nb-whisper-large` (Norwegian
  specialist)"). This makes the specialist visible *before* transcribing, which
  is the direct fix for the original surprise. Requires exposing the generalist
  (`batch-model.txt`) and the specialist map to the frontend.
- **Save-on-transcribe (WYSIWYG).** The transcribe buttons persist the current
  selection to `session-meta` *before* running, so an operator never transcribes
  with a stale set; a small explicit Save covers setting languages without
  transcribing. Inheritance is preserved until the operator actually changes the
  selection and transcribes.
- **Per-run model A/B from the UI is gone.** A/B a generalist by pointing
  `batch-model.txt` at it — the ADR-0010 sanctioned seam — from Settings.
