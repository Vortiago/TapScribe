# Setup — three first-run experiences

## The question

> The current startup/install (`start.sh` → `tools/install_picker.py`) isn't
> great. **What should first-run setup look and feel like instead?**

These are three **disposable, comparable** mockups of the same job — getting a
fresh checkout from "nothing installed" to "dashboard is up, here's your token".
They are **not wired to pip**; machine detection and the install run are mocked
(`src/mock-data.js`, `src/engine.js`) so the three are directly comparable.

Switch with the floating bar, `←`/`→`, or `?variant=A|B|C`. The bar also flips
the **detected machine** (Apple Silicon / NVIDIA / CPU-only) so you can see how
each design adapts the backend (MLX vs CUDA vs CPU) — `?machine=mac|nvidia|cpu`.

## What's wrong with today's flow (the brief)

The current experience is a **terminal TUI** (`install_picker.py`, arrow-keys or
a numbered fallback) launched from `start.sh`, then two more blocking installs
(`[vad]` → PyTorch ~700 MB, `[summarize]`). The pain, from the code:

1. **Expert-only decisions.** "Which backend — CPU / CUDA / MLX?" "Which model
   family?" A first-time operator can't answer these.
2. **Silent multi-minute waits.** `pip install -e ".[vad]"` pulls torch with no
   progress bar; the script just prints one line and goes quiet for minutes.
3. **No estimates.** Nothing tells you the download is ~3 GB or ~4 min, or how
   much disk you'll need.
4. **Secrets flash past once.** The dashboard password + `/tap` token are
   printed to the terminal a single time and scroll away.
5. **Three separate install phases** (picker, vad, summarize) with a probe
   before each.

The shared premise of all three variants: **move setup into the browser**
(served by a tiny stdlib bootstrap before the heavy deps exist), with live
progress and the secrets surfaced as copyable fields.

## The three directions

| Variant | Paradigm | Primary affordance | Bet |
|---|---|---|---|
| **A · One Tap** | Calm consumer card | One recommended plan, one button | 95% never need to read "MLX". Defaults + a disclosure beat a menu. |
| **B · Setup Assistant** | Guided wizard (5 steps) | Pick a **use-case**, not a model | Hand-holding + deferring jargon (Detect → Use → Review → Install → Ready) beats a dense screen. |
| **C · Provision** | Dense dark operator console | Per-family/backend matrix + plan rail | Respect the expert: keep every knob the CLI has, but add the estimates, in-place progress bars, and streaming log it lacks. |
| **D · Centered console** ⭐ | C's dark matrix in A's centered card | Everything surfaced, big "Install & launch" | **The synthesis (default).** Atle's steer: C's substance + TapScribe look (nothing hidden) staged in A's centered, focused install moment. No use-case abstraction, no disclosure. |

All four end on the same thing the terminal does worst: a **Ready** state with
the URL, dashboard password, and `/tap` token as copy-buttoned fields.

They are deliberately **structurally** different (single card vs linear stepper
vs two-pane matrix+rail vs centered matrix), not recolours of one layout. **D is
the current front-runner** — A and B over-simplify (hide what's installed); C and
D surface everything, and D adds A's centered "install" feel. C and D share the
matrix via [`src/console-parts.js`](src/console-parts.js).

## First run vs. manage models (C + D)

The setup screen has two entry contexts — toggle **First run / Manage models** in
the floating bar (`ctx.mode`):

- **First run** — no install exists yet. The real trigger is the absence of the
  install stamp (`.tapscribe-install.json` / the venv stamp `install_picker.py`
  already writes). Shows the full setup; CTA installs everything + launches.
- **Manage models** — a *revisit* to add/change models without re-doing setup.
  Already-installed rows are marked **installed** and locked; only net-new picks
  download (the plan + totals show just the delta); the CTA reads "Install N
  models" and the done state is "Models installed → back to dashboard" (no new
  secrets). Re-running with no changes skips pip — the existing stamp behaviour.

So: first launch drops you here automatically; afterwards you boot straight to
the dashboard and reach this screen on demand (a "Models" entry point) to add
more. No **Presets** row — the matrix is the single, fully-surfaced control.

## Built on vanilla-components

The atoms (button, progress, chip, segmented-control, kv-row, stat-card,
list-row, status-dot, alert, panel, view-header, menu) come from the shared
`vanilla-components` library, vendored verbatim into
[`../_shared/vc/`](../_shared/vc/) and surfaced through
[`../_shared/vc.js`](../_shared/vc.js). Each variant re-themes them with one
`--accent` + a `color-scheme` (A/B light, C/D dark) — same components, four
identities. The variant-specific layout (the card, the wizard stepper, the
dense matrix) stays bespoke.

## Running locally

ES modules need HTTP (not `file://`):

```bash
# from the repo root
python3 -m http.server 8000
# the gallery (all prototypes):  http://localhost:8000/prototypes/
# straight to a variant:         http://localhost:8000/prototypes/setup/?variant=A
```

The gallery (`prototypes/index.html`) links every prototype; a floating
launcher (bottom-right, from `_shared/launcher.js`) hops between them from
inside any prototype.

## Not in scope (intentionally throwaway)

- No real pip / venv / detection — the install is a ~9 s fake.
- No persistence; reload resets.
- The bootstrap-server question ("what runs the page before deps exist?") is a
  real follow-up, not prototyped here. See [NOTES.md](NOTES.md).
