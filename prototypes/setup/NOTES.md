# Verdict

**Question:** What should TapScribe's first-run setup look/feel like, replacing
the `start.sh` → `install_picker.py` terminal flow?

**Decision (2026-06-22): variant D — "Centered console".** Atle picked it. It
feels nicer than the install script during `start`.

## How we got there

**Steer:** A and B hide too much / over-simplify (use-case abstraction,
"Customize" disclosure). C matches TapScribe's design language and *surfaces what
gets installed*, but A has the nicer centered composition + "install feeling".

→ **D** is the synthesis: C's dark matrix (everything surfaced, every backend
tweakable, nothing hidden) staged in A's centered card with A's big "Install &
launch" moment and a celebratory done state. C and D share the matrix via
`src/console-parts.js`.

**Refinements landed:**
- Backend picker shows only concrete host-valid backends with the recommended one
  pre-selected — dropped the confusing `auto·MLX` + `MLX` redundancy.
- Dropped the **Presets** row (C + D) — the matrix is the single surfaced control.
- **First-run vs. manage-models** context (`ctx.mode`): first run = full setup +
  launch; manage = revisit to add models (installed rows locked, only the delta
  downloads, "Install N models", no new secrets). Maps to install-stamp first-run
  detection + a dashboard "Models" entry point.
- Catalog accuracy (mirrors `tapscribe/transcribers/catalog.py`): **Parakeet and
  Voxtral are batch-only**, only Whisper/NB-Whisper stream live — each row shows a
  `live + batch` / `batch only` chip. (The catalog itself was tightened to match:
  `voxtral-mini` → `_BATCH_ONLY` + a regression test.)
- Split **Whisper / NB-Whisper** into separate families — Norwegian is its own
  opt-in row, off by default.

## Before building for real (this is a throwaway mock)

The mock has no tests, minimal error handling, and a vendored `vc/` copy — folding
D into production is a rewrite under real constraints, not a lift-and-ship. Open
problems the mock dodges:

1. **Bootstrap server (the crux).** A browser setup page needs *something* serving
   it before the app's heavy deps exist. Likely a Python stdlib `http.server` +
   SSE/WS shim that drives `pip` and streams progress, started before
   `pip install`. This is the real new infrastructure.
2. **Headless / SSH-only boxes.** The terminal picker works over SSH; the CPU
   Linux-server case is the *least* likely to have a local browser. Keep a
   `--non-interactive` / saved-selection path (`.tapscribe-install.json`) and a
   "open this URL from another machine" affordance — D should be the friendly
   front-end, not the *only* path.
3. **Auth on the setup endpoint.** It drives `pip install` and prints secrets — on
   `--lan` (0.0.0.0) it must be localhost-only or auth-gated, like the dashboard.
4. **Real pip progress is coarse.** The smooth per-package bars are optimistic;
   real streaming means parsing pip output or installing package-by-package.
5. **Read the catalog, don't re-list families.** The mock's family list drifted
   from `catalog.py` twice (Parakeet/Voxtral live tags). The real UI must read
   `/api/models` so it can't drift. The live picker must filter to live-capable
   families (`?context=live` already does).
6. Keep the **skip-install optimization** (unchanged selection → skip pip).
7. Manage-mode only **adds** models; real "manage" probably also wants removal /
   disk reclaim (there's already `DELETE /api/models/cache`).

## When we build it

Fold **D** into a real setup surface; delete A/B/C + the variant switcher + the
floating machine/context toggles (those are prototype scaffolding). Keep the
first-run/manage split and the catalog-driven matrix.
