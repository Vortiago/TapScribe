---
status: accepted
date: 2026-07-19
---

# The Windows Bundle ships an embedded interpreter, not a frozen binary

TapScribe's Windows distribution is the **Bundle** (CONTEXT.md): a per-user
Inno Setup installer carrying an embedded CPython (python-build-standalone),
the built `tapscribe` wheel, and a tray **Launcher**. It is deliberately *not*
a PyInstaller/Nuitka-frozen `.exe`, because two load-bearing facts about the
Recorder require a real interpreter:

1. **`/setup` runs pip at runtime.** Model backends are not baked in; the
   operator picks families in the browser and the picker shells pip via
   `sys.executable`. Under a freeze, `sys.executable` *is* the frozen exe —
   no interpreter to install into, no `site-packages` to install to.
2. **The live channel spawns a real subprocess.** `live.py:_find_exe`
   resolves `whisperlivekit-server` from `PATH`, falling back to
   `Path(sys.prefix)/"Scripts"`. A frozen app has neither.

> **Superseded (#374).** A third point — torch as a multi-GB core dependency
> making a small frozen file unattainable anyway — no longer holds: torch and
> `silero-vad` left the core dependency set for a vendored ONNX model run via
> `onnxruntime` (`tapscribe/vad/`), cutting ~773 MB to an order-of ~150 MB
> baseline. Points 1 and 2 still support the decision and are sufficient; the
> decision stands unchanged.

The embedded interpreter keeps `/setup`, the install picker, and the
WhisperLiveKit child working with no change to the Recorder's runtime
behaviour. Four consequences are decisions in their own right:

**The Bundle installs a wheel, not an editable checkout.** It carries
`tapscribe-X.Y.Z-py3-none-any.whl` and installs *that*, extras resolved from
the wheel's own metadata — PyPI supplies dependencies but never TapScribe
itself, and the shipped installer's version can't drift from the running
server's. Rejected: shipping the source tree so the Bundle *is* a checkout —
zero code changes, but it leaves latent non-checkout bugs (`_PICKER_SCRIPT`,
`pyproject_fingerprint`, `BASE_DIR` resolving into `site-packages`) for the
next non-checkout consumer to trip over.

**The install target is a CLI argument, not an env var or a sniffed
topology.** Three topologies — checkout, Bundle, `pip install tapscribe` —
resolved by `install_target.resolve_install_spec` from an explicit
`--install-spec` threaded through `python -m tapscribe`,
`tapscribe.preflight`, and `tapscribe.install_picker`;
`install_target.pip_install_argv` composes every pip argv from it. Rejected:
auto-detection — "no `pyproject.toml` nearby" guesses wrong on a plain
`pip install tapscribe` in someone's venv.

**The picker and the CUDA-torch swap live in the package**
(`tapscribe/install_picker.py`, `tapscribe/cuda_torch.py`), because `tools/`
is not shipped in the wheel. They stay dependency-free and are only ever run
as a subprocess, never imported by the app — `tapscribe/__init__.py` has no
imports, so `python -m tapscribe.install_picker` costs nothing and preserves
the isolation `setup_install.py`'s docstring requires.

**Install is per-user, with operator data outside the program directory.**
Program and interpreter in `%LOCALAPPDATA%\Programs\TapScribe`;
`TAPSCRIBE_BASE_DIR` points at `%USERPROFILE%\TapScribe`. `Program Files` is
not an option: `/setup` pip-installs into that interpreter at runtime, so the
environment must be writable by the running user, and elevating a process
that also opens a network port is a bad default. Splitting the data out means
uninstalling the program cannot delete someone's meeting recordings.

## Consequences

- **Bring-up logic lives in `tapscribe/preflight.py`** — the repair probes
  and the Windows CUDA torch swap — run by `start.sh`/`start.ps1` and by the
  Launcher as `python -m tapscribe.preflight`, so a check added later cannot
  drift between a PowerShell copy and a C# copy. `PYTHONUNBUFFERED=1`
  deliberately did NOT move: it must be set in the recorder's own
  environment, which a separate preflight process cannot do — `start.ps1`
  sets it, and the Launcher sets it on the child.
- **The saved model selection (`.tapscribe-install.json`) follows
  `BASE_DIR`** (passed to the picker as `--state-file`), not the package —
  beside a wheel-installed package that would be `site-packages`, and a
  Bundle upgrade would lose it. In a checkout `BASE_DIR` *is* the repo root,
  the path devs already had.
- **A skipped model family is visible in the UI.** The picker writes a
  sidecar the dashboard reads and `/setup` says it out loud — stderr in a
  Bundle is a log file nobody opens, so an upgrade retiring a backend would
  otherwise silently stop installing someone's models.
- **A Bundle is not a Bridge.** It never appears in
  `bridges_catalog.BRIDGE_ARTIFACTS` or the dashboard's "Get a bridge" card —
  you need a Bundle to have a dashboard. It rides ADR-0012's release
  mechanism: CI-built, attached to the tagged release under the stable
  unversioned filename `TapScribe-Setup-win-x64.exe`.
- **Two tray icons on a machine running both.** The tray Launcher and the
  tray Bridge stay separate, independently installable executables: a
  Recorder can run on another machine (hence `--lan`/`--tls`), and the
  SpatialChat extension is a Bridge with no tray app at all, so fusing them
  would be correct for one topology and wrong for two. If the same-laptop
  case comes to dominate, the additive fix is a "local Recorder" section in
  the tray Bridge, not a merge.
- **Unsigned for now.** SmartScreen will warn. Signing is chicken-and-egg —
  SignPath Foundation's OSS programme requires a released artifact first —
  so the first Bundle ships unsigned and the application follows it.
- **One Bundle, GPU handled at bring-up.** pip's default Windows torch wheel
  is CPU-only, so preflight's CUDA swap upgrades torch on an NVIDIA box
  (self-gating, cheap elsewhere). A CUDA operator downloads torch twice —
  wasteful, but it keeps the release matrix at one Bundle instead of two.
