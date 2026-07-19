---
status: accepted
date: 2026-07-19
---

# The Windows Bundle ships an embedded interpreter, not a frozen binary

TapScribe is distributed to Windows operators as a **Bundle** (see
CONTEXT.md): a per-user installer carrying an embedded CPython, the
`tapscribe` wheel, and a tray **Launcher**. It is deliberately *not* a
PyInstaller/Nuitka-frozen `.exe`, even though "standalone exe" is what
gets asked for. This ADR records why, and the four seams the choice
forced open.

## Context

Before this, Windows bring-up was `start.ps1` against a git checkout,
which requires the operator to install Python, clone a repo, and run a
PowerShell script. That is a fine developer path and a bad operator path.

The obvious answer — freeze the app into one executable — collides with
three load-bearing facts about how the Recorder actually works:

1. **`/setup` runs pip at runtime.** Model backends are not baked in;
   the operator picks families in the browser and
   `setup_install.picker_install_argv(python=sys.executable)` shells
   pip to install the chosen extras. Under a freeze, `sys.executable`
   *is* the frozen exe — there is no interpreter to install into and no
   `site-packages` to install to.
2. **The live channel spawns a real subprocess.** `live.py:_find_exe`
   resolves `whisperlivekit-server` from `PATH`, falling back to
   `Path(sys.prefix)/"Scripts"`. A frozen app has neither.
3. **`torch` is a core dependency, not an extra.** `silero-vad[onnx-cpu]`
   still imports torch/torchaudio, so any Windows payload is multi-GB
   before a single ASR model is chosen. The headline benefit of a
   freeze — one small self-contained file — is unattainable regardless.

So "one `.exe`, no Python installed" and "keep the runtime model picker"
are close to mutually exclusive, and the property a freeze buys is
cosmetic at this payload size.

## Decision

**Ship an embedded CPython (python-build-standalone) plus the built
wheel, behind a tray Launcher, installed per-user by an Inno Setup
installer.** `sys.executable` stays a real interpreter, so `/setup`, the
install picker, and the WhisperLiveKit child all keep working with no
change to the Recorder's runtime behaviour.

Four consequences are decisions in their own right:

**The Bundle installs a wheel, not an editable checkout.** The picker's
`build_pip_argv` historically hardcoded `pip install -e "."` with
`cwd=REPO_ROOT` — its docstring said outright that "the operator's
TapScribe is a checkout, not a release". The Bundle carries
`tapscribe-X.Y.Z-py3-none-any.whl` and installs *that*, with extras
resolved from its own metadata (so PyPI supplies dependencies but never
TapScribe itself, and the shipped installer's version can't drift from
the running server's). The alternative — ship the source tree so the
Bundle *is* a checkout — needs zero code changes and was rejected
because it buys that by leaving three latent non-checkout bugs in place
(`_PICKER_SCRIPT`, `pyproject_fingerprint`, and `BASE_DIR` resolving
into `site-packages`) for the next non-checkout consumer to trip over.

**The install target is a CLI argument, not an environment variable or
a sniffed topology.** There are now three topologies — checkout,
Bundle, and `pip install tapscribe` from PyPI — and `install_spec()`
resolves between them from an explicit `--install-spec` threaded through
`python -m tapscribe`, `tapscribe.preflight`, and
`tapscribe.install_picker`. Auto-detection would have to guess whether
"no `pyproject.toml` nearby" means Bundle or PyPI-install, and would
guess wrong on a plain `pip install tapscribe` in someone's venv.

**The picker and the CUDA-torch swap move into the package.** `tools/`
is not shipped in the wheel (`packages.find` includes `tapscribe*`
only), so `tools/install_picker.py` and `tools/ensure_cuda_torch.py`
become `tapscribe/install_picker.py` and `tapscribe/cuda_torch.py`.
They stay dependency-free and are still only ever run as a subprocess,
never imported by the app — `tapscribe/__init__.py` is a docstring and
`__version__` with no imports, so `python -m tapscribe.install_picker`
costs nothing and preserves the isolation `setup_install.py`'s docstring
requires.

**Install is per-user, with operator data outside the program
directory.** Program and the embedded interpreter go to
`%LOCALAPPDATA%\Programs\TapScribe`; `TAPSCRIBE_BASE_DIR` points at
`%USERPROFILE%\TapScribe`. `Program Files` is not an option: `/setup`
pip-installs into that interpreter at runtime, so the environment must be
writable by the running user, and elevating a process that also opens a
network port to work around that is a bad default. Splitting the data
out additionally means uninstalling the program cannot delete someone's
meeting recordings.

## Consequences

- **`start.ps1`'s bring-up logic needed a shared home.** Three of its
  steps are load-bearing but homeless in a Bundle: the Windows CUDA
  torch swap (`start.ps1` only — the Linux torch wheel bundles CUDA),
  the silero-vad repair probe, and the `[summarize]` extra probe. These
  moved to `tapscribe/preflight.py`, which both `start.ps1` and the
  Launcher call, so a fourth check added later cannot drift between a
  PowerShell copy and a C# copy. `PYTHONUNBUFFERED=1` deliberately did
  NOT move: it has to be set in the recorder's own environment, which a
  separate preflight process cannot do — `start.ps1` sets it, and the
  Launcher sets it on the child.
- **The saved model selection moved to the data directory.**
  `.tapscribe-install.json` was anchored beside the package, which in a
  wheel install is `site-packages`; it now follows `BASE_DIR` (passed to
  the picker as `--state-file`) so a Bundle upgrade can't lose it. In a
  checkout `BASE_DIR` *is* the repo root, so devs see the same path as
  before.
- **A skipped model family is now visible in the UI.**
  `removed_backend_families()` warned only to stderr — which in a Bundle
  is a log file nobody opens, so an upgrade that retires a backend would
  silently stop installing someone's models. The picker now writes a
  sidecar the dashboard reads, and `/setup` says it out loud.
- **A Bundle is not a Bridge.** It never appears in
  `bridges_catalog.BRIDGE_ARTIFACTS` or the dashboard's "Get a bridge"
  card — you need a Bundle to have a dashboard, so a dashboard cannot
  advertise one. It still rides ADR-0012's mechanism: CI-built, attached
  to the tagged release under a stable unversioned filename
  (`TapScribe-Setup-win-x64.exe`), announced by the README and the
  Release page.
- **Two tray icons on a machine running both.** The tray Launcher and
  the tray Bridge are separate executables. They are independently
  installable — a Recorder can run on another machine (hence `--lan` /
  `--tls`), and the SpatialChat extension is a Bridge that needs no tray
  app at all — so fusing them would be correct for one topology and
  wrong for two. If the same-laptop case comes to dominate, the additive
  fix is the tray Bridge growing a "local Recorder" section, not a merge.
- **Unsigned for now.** SmartScreen will warn. Signing is chicken-and-egg
  — SignPath Foundation's OSS programme requires the artifact to already
  be released in the form to be signed — so the first Bundle ships
  unsigned and the application follows it.
- **The GPU story is unchanged but relocated.** The Bundle bakes CPU
  torch (PyPI's default Windows wheel); preflight's CUDA swap upgrades it
  on an NVIDIA box exactly as `start.ps1` did. A CUDA operator's first
  `/setup` therefore re-downloads torch — wasteful, but identical to what
  a dev checkout does today, and it keeps the release matrix at one
  Bundle instead of two.
