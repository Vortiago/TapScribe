"""The `/tap` wire contract is one contract in four languages and six prose
files. This is what keeps them honest (#356).

`tapscribe/` is the SOURCE: the Recorder serves `/tap`, so its constants are
the contract by definition. `tools/stamp_tap_wire.py` reads them and stamps
the literal into each Bridge and each doc; this file fails when a site drifts
from the Recorder — which is what catches a hand edit that skipped the
stamper.

Three tiers, with different authority:

  1. **Wire contract** — enforced. Subprotocol prefix, sample rate, channels,
     sample width, frame samples/bytes, `__probe__`. Compared against the
     Recorder's IMPORTED constants, never a copy, so the gate and the server
     cannot diverge.
  2. **Blip-resilience recipe** — recommended, not enforced (CONTEXT.md). The
     Recorder has no opinion on the backoff ladder / gap buffer / drain
     budget; a third-party Bridge may deviate. What is pinned is that the
     BUNDLED Bridges and the docs don't drift from each other.
  3. **Completeness** — the tripwire that proves this table describes reality.
     Tiers 1 and 2 only prove the sites they NAME agree; a declaration site
     nobody listed is invisible to them, which is exactly how the first draft
     of #356 missed six of them. `test_declared_sites_match_the_repo` sweeps
     the tree and fails both directions.

Deliberately NOT here: close code 4401 (the Recorder emits it as an unnamed
literal in `routes/tap.py`, so there is nothing to stamp FROM; the tray
bridge never branches on the numeric; and it fails loudly anyway — its JS
behaviour is pinned in `spacialchat-bridge/tests/wire-contract.test.js`), and
the first-connect-failure semantics, which `bridges/README.md` documents as a
deliberate per-Bridge choice. See ADR-0019.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# tools/ isn't a package — same path insert tests/test_package_bridge.py uses.
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import stamp_tap_wire as stamper  # noqa: E402

# ---------------------------------------------------------------------------
# The extractor — a pure function over file text
# ---------------------------------------------------------------------------


def test_reads_a_python_assignment() -> None:
    """The simplest shape, and the one the Recorder itself uses."""
    site = stamper.Site(
        path=Path("local_test_bridge.py"),
        key="subprotocol_prefix",
        symbol="TAP_SUBPROTOCOL_PREFIX",
        pattern=stamper.py_assign("TAP_SUBPROTOCOL_PREFIX"),
    )
    text = 'FRAME_BYTES = 640\nTAP_SUBPROTOCOL_PREFIX = "tapscribe.v1.tap."\n'
    assert stamper.declared_value(text, site) == "tapscribe.v1.tap."


def test_a_renamed_symbol_raises_instead_of_reading_nothing() -> None:
    """The property that stops this whole gate failing OPEN.

    A rename upstream makes the anchor stop matching. If that returned None
    the comparison downstream would be `None == None` across every site and
    the suite would go green while checking nothing. It must raise, and the
    message must name the file and the symbol so the fix is obvious.
    """
    site = stamper.Site(
        path=Path("local_test_bridge.py"),
        key="subprotocol_prefix",
        symbol="TAP_SUBPROTOCOL_PREFIX",
        pattern=stamper.py_assign("TAP_SUBPROTOCOL_PREFIX"),
    )
    with pytest.raises(stamper.AnchorNotFound) as excinfo:
        stamper.declared_value('RENAMED_PREFIX = "tapscribe.v1.tap."\n', site)

    message = str(excinfo.value)
    assert "local_test_bridge.py" in message
    assert "TAP_SUBPROTOCOL_PREFIX" in message


def test_reads_a_csharp_int_through_its_digit_separator() -> None:
    """`TapWire.cs` writes `16_000` where Python writes `16000`.

    The same fact, two spellings. The extractor returns the CANONICAL value so
    the comparison is over facts, not over formatting — otherwise every site
    would need the Recorder's exact punctuation, which C# style forbids.
    """
    site = stamper.Site(
        path=Path("TapWire.cs"),
        key="sample_rate",
        symbol="SampleRate",
        pattern=stamper.cs_const("SampleRate"),
        spelling=stamper.CS_INT,
    )
    text = "    public const int SampleRate = 16_000;\n"
    assert stamper.declared_value(text, site) == 16000


@pytest.mark.parametrize(
    ("declared", "expected_ms"),
    [
        ("TimeSpan.FromSeconds(5)", 5000),
        ("TimeSpan.FromSeconds(8)", 8000),
        ("TimeSpan.FromMilliseconds(200)", 200),
        ("TimeSpan.FromMilliseconds(3200)", 3200),
    ],
)
def test_reads_a_csharp_timespan_as_milliseconds(declared: str, expected_ms: int) -> None:
    """`TapStreamOptions.cs` spells the recipe in TimeSpans, `content.js` in
    bare milliseconds. Canonical is milliseconds, so `FromSeconds(8)` and
    `DRAIN_MAX_MS = 8000` are recognisably the same budget."""
    site = stamper.Site(
        path=Path("TapStreamOptions.cs"),
        key="drain_budget_ms",
        symbol="DrainBudget",
        pattern=stamper.cs_property("DrainBudget"),
        spelling=stamper.CS_TIMESPAN_MS,
    )
    text = f"    public TimeSpan DrainBudget {{ get; init; }} = {declared};\n"
    assert stamper.declared_value(text, site) == expected_ms


def test_a_gate_only_site_refuses_to_be_stamped() -> None:
    """Tier 2 is gated, not stamped — the Recorder has no opinion on the
    Blip-resilience recipe, so nothing may write it FROM the Recorder. A
    read-only Spelling has no `render`, which makes that unwritable rather
    than merely documented."""
    assert stamper.CS_TIMESPAN_MS.render is None
    site = stamper.Site(
        path=Path("TapStreamOptions.cs"),
        key="drain_budget_ms",
        symbol="DrainBudget",
        pattern=stamper.cs_property("DrainBudget"),
        spelling=stamper.CS_TIMESPAN_MS,
    )
    with pytest.raises(stamper.NotStampable):
        stamper.restamped("    public TimeSpan DrainBudget { get; init; } = x;\n", site, 9000)


def test_reads_a_js_const_string() -> None:
    site = stamper.Site(
        path=Path("control-client.js"),
        key="subprotocol_prefix",
        symbol="TAP_SUBPROTOCOL_PREFIX",
        pattern=stamper.js_const("TAP_SUBPROTOCOL_PREFIX"),
    )
    text = '  const TAP_SUBPROTOCOL_PREFIX = "tapscribe.v1.tap.";\n'
    assert stamper.declared_value(text, site) == "tapscribe.v1.tap."


def test_reads_a_js_const_int() -> None:
    site = stamper.Site(
        path=Path("content.js"),
        key="max_buffer_bytes",
        symbol="MAX_BUFFER_BYTES",
        pattern=stamper.js_const("MAX_BUFFER_BYTES"),
        spelling=stamper.INT,
    )
    assert stamper.declared_value("  const MAX_BUFFER_BYTES = 96000;\n", site) == 96000


def test_reads_the_backoff_ladder_from_both_languages_as_one_list() -> None:
    """`content.js` writes bare milliseconds in an array literal;
    `TapStreamOptions.cs` writes a collection expression of TimeSpan factory
    calls across six lines. Canonical is `[200, 400, 800, 1600, 3200]` for
    both, or the two could never be compared."""
    js = stamper.Site(
        path=Path("content.js"),
        key="backoff_ms",
        symbol="BACKOFF_MS",
        pattern=stamper.js_const("BACKOFF_MS"),
        spelling=stamper.INT_LIST,
    )
    assert stamper.declared_value("  const BACKOFF_MS = [200, 400, 800, 1600, 3200];\n", js) == [
        200,
        400,
        800,
        1600,
        3200,
    ]

    cs = stamper.Site(
        path=Path("TapStreamOptions.cs"),
        key="backoff_ms",
        symbol="Backoff",
        pattern=stamper.cs_property("Backoff"),
        spelling=stamper.CS_TIMESPAN_LIST_MS,
    )
    text = (
        "    public IReadOnlyList<TimeSpan> Backoff { get; init; } =\n"
        "    [\n"
        "        TimeSpan.FromMilliseconds(200),\n"
        "        TimeSpan.FromMilliseconds(400),\n"
        "        TimeSpan.FromMilliseconds(800),\n"
        "        TimeSpan.FromMilliseconds(1600),\n"
        "        TimeSpan.FromMilliseconds(3200),\n"
        "    ];\n"
    )
    assert stamper.declared_value(text, cs) == [200, 400, 800, 1600, 3200]


def test_prose_matchers_must_be_anchored_on_words_not_just_digits() -> None:
    """320 and 640 appear as ordinary numbers in four prose files. A prose
    pattern that anchors only on punctuation would rewrite whichever one it
    hit first, so `prose()` refuses to build one — the plan's "never a bare
    numeric scan" rule, made unwritable instead of merely documented."""
    with pytest.raises(ValueError, match="anchor"):
        stamper.anchored(r"\((?P<v>\d+)\)")

    with pytest.raises(ValueError, match="'v'"):
        stamper.anchored(r"(\d+) samples")

    # Anchored on the surrounding words: fine.
    pattern = stamper.anchored(r"samples = (?P<v>\d+) bytes")
    assert pattern.search("(320 samples = 640 bytes)").group("v") == "640"


def test_reads_prose_that_states_the_rate_in_kilohertz() -> None:
    """`bridges/README.md` says "16 kHz mono", not "16000". Same fact, reader's
    units — so a rate change has to rewrite the prose as `48 kHz`."""
    site = stamper.Site(
        path=Path("README.md"),
        key="sample_rate",
        symbol="16 kHz mono",
        pattern=stamper.anchored(r"(?P<v>\d+) kHz mono"),
        spelling=stamper.KHZ,
    )
    text = "**Audio format:** PCM signed 16-bit little-endian, 16 kHz mono, raw\n"
    assert stamper.declared_value(text, site) == 16000
    assert "48 kHz mono" in stamper.restamped(text, site, 48000)


def test_a_repeated_declaration_is_read_once_and_stamped_everywhere() -> None:
    """`16 kHz mono` appears three times in the tray README and twice in
    `bridges/README.md`. A doc that states the fact five times must state the
    NEW fact five times — stamping only the first occurrence is precisely the
    stale-prose drift this gate exists to stop."""
    site = stamper.Site(
        path=Path("README.md"),
        key="sample_rate",
        symbol="16 kHz mono",
        pattern=stamper.anchored(r"(?P<v>\d+) kHz mono"),
        spelling=stamper.KHZ,
    )
    text = "a resampler to 16 kHz mono int16\n...\nraw PCM, 16 kHz mono int16, 20 ms\n"

    assert stamper.declared_value(text, site) == 16000
    assert stamper.restamped(text, site, 48000).count("48 kHz mono") == 2


def test_a_file_that_disagrees_with_itself_is_an_error() -> None:
    """Two occurrences, two values — the file has already drifted internally.
    Returning either one would let the gate pick the convenient answer, so
    this raises instead."""
    site = stamper.Site(
        path=Path("README.md"),
        key="sample_rate",
        symbol="16 kHz mono",
        pattern=stamper.anchored(r"(?P<v>\d+) kHz mono"),
        spelling=stamper.KHZ,
    )
    with pytest.raises(stamper.SiteDisagreesWithItself, match="16000.*48000|48000.*16000"):
        stamper.declared_value("16 kHz mono here, 48 kHz mono there\n", site)


# ---------------------------------------------------------------------------
# Tier 1 — the Wire contract, against the Recorder's own constants
# ---------------------------------------------------------------------------


def _read(site: stamper.Site) -> object:
    return stamper.declared_value((REPO_ROOT / site.path).read_text(encoding="utf-8"), site)


@pytest.mark.parametrize("site", stamper.STAMPS, ids=lambda s: f"{s.path.name}:{s.key}")
def test_every_stamped_site_matches_the_recorder(site: stamper.Site) -> None:
    """The gate proper. A Bridge edited by hand, without running the stamper,
    fails here — naming the file, the symbol and both values."""
    expected = stamper.recorder_contract()[site.key]
    assert _read(site) == expected, (
        f"{site.path} declares {site.symbol} = {_read(site)!r}, but the Recorder's "
        f"{site.key!r} is {expected!r}. Run `python3 tools/stamp_tap_wire.py`."
    )


def test_the_stamp_table_covers_every_bridge_language() -> None:
    """A wire change must reach all four languages. If a Bridge stops being
    represented here the gate would still pass while that Bridge rots."""
    suffixes = {site.path.suffix for site in stamper.STAMPS}
    assert {".cs", ".js", ".py", ".md"} <= suffixes


# ---------------------------------------------------------------------------
# Tier 2 — the Blip-resilience recipe (recommended, not enforced)
# ---------------------------------------------------------------------------

#: What the two bundled Bridges converged on. NOT enforced on a third-party
#: Bridge — the Recorder has no opinion (CONTEXT.md, "Blip-resilience
#: recipe"). Frozen here so the bundled ones can't drift from each other or
#: from the docs. Same role as `test_route_surface.py`'s `_GOLDEN`.
_RECOMMENDED: dict[str, object] = {
    "backoff_ms": [200, 400, 800, 1600, 3200],
    "backoff_cap_ms": 5000,
    "backoff_jitter": 0.25,
    "max_buffer_bytes": 96000,
    "drain_budget_ms": 8000,
}


@pytest.mark.parametrize("site", stamper.RECIPE, ids=lambda s: f"{s.path.name}:{s.key}")
def test_the_blip_resilience_recipe_is_the_same_everywhere(site: stamper.Site) -> None:
    assert _read(site) == _RECOMMENDED[site.key], (
        f"{site.path} declares {site.symbol} = {_read(site)!r}; the recipe says "
        f"{_RECOMMENDED[site.key]!r}. A third-party Bridge may deviate — the "
        f"bundled ones may not."
    )


def test_the_recipe_table_names_exactly_what_the_sites_declare() -> None:
    """No orphan row on either side: a golden value nothing reads is dead
    weight, and a site whose key isn't in the table is unchecked."""
    assert {site.key for site in stamper.RECIPE} == set(_RECOMMENDED)


# ---------------------------------------------------------------------------
# Anti-vacuity — is each site actually load-bearing?
# ---------------------------------------------------------------------------
#
# Every conformance assertion above is green on arrival, because nothing is
# drifted today. Green-on-arrival is exactly the shape a vacuous test has, so
# these prove the difference: perturb the REAL file text in memory, one site
# at a time, and require the gate to notice. A pattern anchored on the wrong
# occurrence, or one whose value the comparison never actually reads, survives
# the perturbation and fails here.
#
# Nothing on disk is touched.

_DIGITS = re.compile(r"\d+")


def _perturb(raw: str) -> str:
    """`raw` altered so it still parses but states a different fact.

    Bumping the first number covers every numeric spelling at once —
    `320`, `16_000`, `96 000`, `[200, 400, ...]`, `TimeSpan.FromSeconds(5)`.
    A value with no digits at all (`__probe__`) gets a suffix instead.
    """
    m = _DIGITS.search(raw)
    if m:
        return raw[: m.start()] + str(int(m.group()) + 1) + raw[m.end() :]
    return raw.rstrip('"') + 'X"' if raw.endswith('"') else raw + "X"


def _conforms(text: str, site: stamper.Site, expected: object) -> bool:
    """Would the gate accept `text` for this site? Any extractor failure counts
    as a rejection — a renamed anchor and a wrong value are both drift."""
    try:
        return stamper.declared_value(text, site) == expected
    except (stamper.AnchorNotFound, stamper.SiteDisagreesWithItself, ValueError):
        return False


_ALL_SITES = [
    *[(s, "wire") for s in stamper.STAMPS],
    *[(s, "recipe") for s in stamper.RECIPE],
]


@pytest.mark.parametrize(
    ("site", "tier"), _ALL_SITES, ids=lambda x: x if isinstance(x, str) else f"{x.path.name}:{x.key}"
)
def test_perturbing_a_site_makes_the_gate_reject_it(site: stamper.Site, tier: str) -> None:
    expected = stamper.recorder_contract()[site.key] if tier == "wire" else _RECOMMENDED[site.key]
    text = (REPO_ROOT / site.path).read_text(encoding="utf-8")
    assert _conforms(text, site, expected), "precondition: the real file conforms"

    match = site.pattern.search(text)
    assert match is not None
    start, end = match.span("v")
    perturbed = text[:start] + _perturb(match.group("v")) + text[end:]

    assert not _conforms(perturbed, site, expected), (
        f"{site.path}: perturbing {site.symbol} did not make the gate fail, so "
        f"this row proves nothing. Its pattern is anchored on the wrong "
        f"occurrence, or the comparison never reads what it captures."
    )


# ---------------------------------------------------------------------------
# Tier 3 — completeness. Does the table above describe reality?
# ---------------------------------------------------------------------------
#
# Tiers 1 and 2 only check the sites they NAME. A declaration nobody listed is
# invisible to them — which is how the first cut of #356 missed six real ones,
# including a whole second Python copy in `tests/e2e/harness.py` sitting under
# a comment that was itself already false. So: sweep the tree and fail both
# directions.
#
# WHAT THIS DOES NOT COVER, stated plainly rather than implied: a broad sweep
# for the contract's NUMBERS ("16 kHz", "640 bytes") finds 206 hits across 74
# files, nearly all of them incidental prose in modules that merely handle
# recorder-format audio. An exempt list that long would be worse than no gate.
# So the sweep is narrowed to the two shapes that actually go stale silently:
#
#   A. a module-level constant NAMED like a wire constant, anywhere outside a
#      declared site — the `harness.py` shape, i.e. a fresh private copy;
#   B. the subprotocol literal spelled out in shipped code or docs — the tray
#      README / `TapClient.cs` shape, i.e. a prose restatement.
#
# Incidental prose ("this module reads 16 kHz mono WAVs") is deliberately out
# of scope: it describes the format, it doesn't declare it.

#: The Recorder's own modules. Not "sites" — the gate READS these by import,
#: so they can't drift from themselves.
_SOURCE_MODULES = {
    "tapscribe/auth.py",
    "tapscribe/audio.py",
    "tapscribe/speech_gate.py",
    "tapscribe/config.py",
    "tapscribe/tap_fan_out.py",
}

#: Names that would make a private copy of a wire constant.
_CONTRACT_SYMBOLS = (
    "SAMPLE_RATE",
    "FRAME_SAMPLES",
    "FRAME_BYTES",
    "TAP_SUBPROTOCOL_PREFIX",
    "BACKOFF_MS",
    "BACKOFF_CAP_MS",
    "MAX_BUFFER_BYTES",
    "DRAIN_MAX_MS",
    "SubprotocolPrefix",
    "SampleRate",
    "FrameSamples",
    "FrameBytes",
    "MaxBufferBytes",
    "DrainBudget",
    "BackoffCap",
    "BackoffJitter",
)

_PY_CS_DECL = re.compile(
    r"^(?:\s*(?:public\s+)?(?:const\s+\w+\s+|\w+\s+)?)?("
    + "|".join(_CONTRACT_SYMBOLS)
    + r")\s*(?::\s*\w+\s*)?=(?!=)",
    re.MULTILINE,
)
_JS_DECL = re.compile(r"^\s*const\s+(" + "|".join(_CONTRACT_SYMBOLS) + r")\s*=(?!=)", re.MULTILINE)
_SUBPROTOCOL_LITERAL = re.compile(r"tapscribe\.v\d+\.tap\.")

#: Declarations that are NOT the wire contract, each with the reason. A new
#: entry here is a claim that has to survive review — that is the point.
_EXEMPT_DECLARATIONS: dict[str, str] = {
    "bridges/windows-tray-bridge/tests/TapScribe.Bridge.Core.Tests/DrainBarrierTests.cs": "TapStreamOptions overrides with deliberately tight values so the "
    "resilience paths run in milliseconds — a test fixture, not a declaration.",
    "bridges/windows-tray-bridge/tests/TapScribe.Bridge.Core.Tests/TapStreamTests.cs": "Same: per-test TapStreamOptions overrides.",
    "bridges/windows-tray-bridge/tests/TapScribe.Bridge.Core.Tests/TestDoubles.cs": "Same: shared test-double TapStreamOptions.",
    "tests/test_vad_silero_port.py": "Silero VAD requires 16 kHz for its OWN reasons, independent of /tap. "
    "Same number, different fact — coupling it to the wire contract would "
    "be wrong, since a /tap rate change must not silently retune the VAD.",
}


def _tracked_files(suffixes: frozenset[str]) -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return [Path(p) for p in out.stdout.split() if Path(p).suffix in suffixes]


def test_no_undeclared_copy_of_a_wire_constant() -> None:
    """Sweep A: a module-level constant named like a wire constant, outside
    any declared site. This is the `harness.py` shape — a private copy that
    tiers 1 and 2 would never look at."""
    declared = {str(site.path) for site in (*stamper.STAMPS, *stamper.RECIPE)}
    offenders: dict[str, list[str]] = {}
    for path in _tracked_files(frozenset({".py", ".js", ".cs"})):
        key = str(path)
        if key in declared or key in _SOURCE_MODULES or key in _EXEMPT_DECLARATIONS:
            continue
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        names = {m.group(1) for m in _PY_CS_DECL.finditer(text)}
        names |= {m.group(1) for m in _JS_DECL.finditer(text)}
        if names:
            offenders[key] = sorted(names)

    assert not offenders, (
        "these files declare their own copy of a wire constant:\n"
        + "\n".join(f"  {f}: {n}" for f, n in sorted(offenders.items()))
        + "\nImport it (Python on the Recorder's side), add it to STAMPS in "
        "tools/stamp_tap_wire.py (another language), or exempt it with a "
        "written reason in _EXEMPT_DECLARATIONS."
    )


def test_no_unstamped_copy_of_the_subprotocol_literal() -> None:
    """Sweep B: `tapscribe.vN.tap.` spelled out in shipped code or docs where
    no declared site covers it. This is what found the tray README's wire
    summary and `TapClient.cs`'s XML doc comment."""
    covered: dict[str, list[tuple[int, int]]] = {}
    for site in (*stamper.STAMPS, *stamper.RECIPE):
        text = (REPO_ROOT / site.path).read_text(encoding="utf-8")
        covered.setdefault(str(site.path), []).extend(m.span() for m in site.pattern.finditer(text))

    offenders: list[str] = []
    for path in _tracked_files(frozenset({".py", ".js", ".cs", ".md"})):
        key = str(path)
        # Tests are CONSUMERS: a stale literal there fails that test loudly.
        if key in _SOURCE_MODULES or "tests/" in key or key.startswith("tests/"):
            continue
        text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
        spans = covered.get(key, [])
        for m in _SUBPROTOCOL_LITERAL.finditer(text):
            if any(a <= m.start() and m.end() <= b for a, b in spans):
                continue
            offenders.append(f"  {key}:{text[: m.start()].count(chr(10)) + 1}")

    assert not offenders, (
        "the subprotocol literal is spelled out where no stamp covers it:\n"
        + "\n".join(offenders)
        + "\nAdd a Site row in tools/stamp_tap_wire.py, or name the constant "
        "instead of restating the literal."
    )


def test_every_exemption_still_applies() -> None:
    """An exemption whose file is gone, or which no longer declares anything,
    is stale permission. Expire it rather than letting it accumulate."""
    for key, reason in _EXEMPT_DECLARATIONS.items():
        path = REPO_ROOT / key
        assert path.exists(), f"exempt file no longer exists: {key}"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert _PY_CS_DECL.search(text) or _JS_DECL.search(text), (
            f"{key} no longer declares a wire-constant name, so its exemption "
            f"is dead — drop it. (Reason on file: {reason})"
        )


# ---------------------------------------------------------------------------
# The stamper's write half
# ---------------------------------------------------------------------------


def _worktree(tmp_path: Path) -> Path:
    """A copy of every file the stamper writes, under tmp_path."""
    for site in stamper.STAMPS:
        dest = tmp_path / site.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((REPO_ROOT / site.path).read_bytes())
    return tmp_path


def test_stamping_a_clean_tree_changes_nothing(tmp_path: Path) -> None:
    """Idempotence, and the reason the gate is green on arrival: the repo is
    already stamped, so running the tool is a no-op."""
    root = _worktree(tmp_path)
    before = {s.path: (root / s.path).read_bytes() for s in stamper.STAMPS}

    assert stamper.stamp(root) == []

    for path, blob in before.items():
        assert (root / path).read_bytes() == blob, f"{path} was rewritten needlessly"


def test_stamping_repairs_a_drifted_site_and_only_that_site(tmp_path: Path) -> None:
    """The whole point of the tool: one hand-edited Bridge, one command."""
    root = _worktree(tmp_path)
    target = next(s for s in stamper.STAMPS if s.path.name == "TapWire.cs" and s.key == "frame_samples")
    drifted = stamper.restamped((root / target.path).read_text(encoding="utf-8"), target, 999)
    (root / target.path).write_text(drifted, encoding="utf-8")
    others = {s.path: (root / s.path).read_bytes() for s in stamper.STAMPS if s.path != target.path}

    changed = stamper.stamp(root)

    assert changed == [target.path]
    assert (
        stamper.declared_value((root / target.path).read_text(encoding="utf-8"), target)
        == stamper.recorder_contract()["frame_samples"]
    )
    for path, blob in others.items():
        assert (root / path).read_bytes() == blob, f"{path} should not have been touched"


def test_the_stamper_never_writes_the_recorder() -> None:
    """`tapscribe/` is the SOURCE. If the tool could rewrite it, a typo in a
    Bridge could propagate INTO the contract instead of being caught by it."""
    assert not any(str(site.path).startswith("tapscribe/") for site in stamper.STAMPS)


def test_derived_values_are_never_stamped() -> None:
    """`FRAME_BYTES = FRAME_SAMPLES * 2` is a derivation in three languages.
    Stamping it would replace the derivation with a literal that then has to
    be maintained."""
    derived = [s for s in stamper.STAMPS if s.key == "frame_bytes" and s.path.suffix != ".md"]
    assert derived == [], (
        "frame_bytes is stamped into CODE at "
        f"{[str(s.path) for s in derived]} — code derives it from frame_samples; "
        "only prose spells it out."
    )
