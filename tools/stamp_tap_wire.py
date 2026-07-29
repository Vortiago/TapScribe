#!/usr/bin/env python3
"""Stamp the `/tap` wire constants from the Recorder into every Bridge.

The Recorder serves `/tap`, so `tapscribe/` is the contract by definition:
this tool READS its constants and WRITES the matching literal into each
Bridge and each doc that restates it. It never writes `tapscribe/` — a wire
change is a hand edit there (a real server behaviour change, with its own
tests), then::

    python3 tools/stamp_tap_wire.py

Same shape as ``tools/bump_version.py``, and the same promise: **a new Bridge
is one new ``Site`` row.** ``tests/test_tap_wire_contract.py`` is the gate
that fails when a site drifts — including a hand edit that skipped this tool.

Idempotent, stdlib only. Values are replaced IN PLACE so file formatting is
preserved.

Two rules the table must keep:

* **Never stamp a derived value.** ``FRAME_BYTES = FRAME_SAMPLES * 2`` is a
  derivation in Python, C# and the e2e harness alike; replacing it with a
  literal would lose the derivation.
* **Every pattern is symbol- or markup-anchored.** 320 and 640 appear in four
  prose files as ordinary numbers; a bare numeric scan would rewrite the wrong
  one. Each pattern names its symbol (``FRAME_SAMPLES =``) or its markup
  (``**96 000 bytes**``).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Spelling:
    """How one site writes a canonical value.

    The contract is over FACTS, not formatting: C# writes ``16_000`` where
    Python writes ``16000`` and `bridges/README.md` writes ``16 kHz``. Each is
    the same fact spelled for its own reader, so each site declares how to
    `parse` its spelling back to canonical and how to `render` canonical into
    it. Comparing raw text instead would force every site to adopt the
    Recorder's punctuation, which the C# style rules forbid outright.

    ``render`` is ``None`` for a READ-ONLY site. That is not an omission: the
    Blip-resilience recipe (tier 2) is gated but never stamped, because the
    Recorder has no opinion on it — there is nothing to stamp FROM. Leaving
    `render` off makes "this site is gate-only" unwritable rather than merely
    documented.
    """

    parse: Callable[[str], Any]
    render: Callable[[Any], str] | None = None


def _timespan_ms(declared: str) -> int:
    """`TimeSpan.FromSeconds(8)` / `TimeSpan.FromMilliseconds(200)` -> ms."""
    m = re.fullmatch(r"TimeSpan\.From(Seconds|Milliseconds)\(([\d._]+)\)", declared.strip())
    if m is None:
        raise ValueError(f"not a TimeSpan factory call: {declared!r}")
    amount = float(m.group(2).replace("_", ""))
    return int(amount * 1000) if m.group(1) == "Seconds" else int(amount)


def _unquote(declared: str) -> str:
    s = declared.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    raise ValueError(f"expected a quoted string literal, got {declared!r}")


#: A double-quoted string literal — the same syntax in Python, JS and C#.
TEXT = Spelling(parse=_unquote, render=lambda v: f'"{v}"')

#: A plain integer: Python, JavaScript.
INT = Spelling(parse=int, render=str)

#: A C# integer literal, which conventionally carries `_` digit separators.
CS_INT = Spelling(parse=lambda s: int(s.replace("_", "")), render=lambda v: f"{v:_}")

#: A C# TimeSpan factory call, canonicalised to milliseconds so it compares
#: against `content.js`'s bare `DRAIN_MAX_MS = 8000`. Read-only: tier 2.
CS_TIMESPAN_MS = Spelling(parse=_timespan_ms)

#: Prose that states the rate in kilohertz — "16 kHz mono".
KHZ = Spelling(parse=lambda s: int(s) * 1000, render=lambda v: str(v // 1000))

#: Prose that states a duration in whole seconds — "(8 s)".
SECONDS_MS = Spelling(parse=lambda s: int(s) * 1000, render=lambda v: str(v // 1000))

#: Prose that spaces its thousands — "**96 000 bytes**".
SPACED_INT = Spelling(parse=lambda s: int(s.replace(" ", "")), render=lambda v: f"{v:,}".replace(",", " "))

#: Prose that states a fraction as a percentage — "**±25 % jitter**".
PERCENT = Spelling(parse=lambda s: float(s) / 100)

#: A JS array literal of integers. Read-only: tier 2.
INT_LIST = Spelling(parse=lambda s: [int(p) for p in re.findall(r"-?\d+", s)])

#: A bare float — `public double BackoffJitter { get; init; } = 0.25;`.
FLOAT = Spelling(parse=float)

#: A value embedded in a larger literal (a URL query string), so it carries no
#: quotes of its own — `"/tap?identity=__probe__&name=probe"`.
RAW = Spelling(parse=str.strip, render=str)


def readonly(spelling: Spelling) -> Spelling:
    """The same spelling with no `render` — a gate-only site.

    Tier-2 sites are read by the gate and never written by the stamper, and
    the two tables below enforce that structurally (the stamper only iterates
    `STAMPS`). This is the second lock: even a row moved into the wrong table
    by hand cannot be written.
    """
    return Spelling(parse=spelling.parse)


#: A C# collection expression of TimeSpan factory calls, canonicalised to a
#: list of milliseconds so it compares against `BACKOFF_MS`. Read-only: tier 2.
CS_TIMESPAN_LIST_MS = Spelling(
    parse=lambda s: [_timespan_ms(call) for call in re.findall(r"TimeSpan\.From\w+\([\d._]+\)", s)]
)


class NotStampable(TypeError):
    """A read-only site was asked to be written.

    Tier-2 sites (the Blip-resilience recipe) are gated, not stamped — see
    `Spelling`. Reaching here means a `Site` row was added to the stamp table
    that belongs in the gate's golden table instead.
    """


class SiteDisagreesWithItself(ValueError):
    """One file declares the same value twice, differently.

    Docs restate the wire format several times (`16 kHz mono` appears three
    times in the tray README). Picking one occurrence would let the gate
    answer with whichever happened to be first, hiding drift that has already
    happened inside a single file.
    """


class AnchorNotFound(LookupError):
    """A site's pattern matched nothing.

    Raised rather than returning ``None`` because ``None`` would compare equal
    to the next un-anchored site and turn the whole gate green while checking
    nothing. A rename upstream must be loud.
    """


@dataclass(frozen=True)
class Site:
    """One place that declares one contract value.

    ``pattern`` must capture the value in a group named ``v``, anchored on the
    symbol or markup around it so it can't match a coincidental number.
    ``symbol`` is what the anchor is named in that file, for the error message.
    """

    path: Path
    key: str
    symbol: str
    pattern: re.Pattern[str]
    spelling: Spelling = field(default=TEXT)


def py_assign(symbol: str) -> re.Pattern[str]:
    """A module-level Python assignment — `SAMPLE_RATE = 16000`.

    Every pattern here captures the RAW right-hand side; unquoting and unit
    conversion belong to the `Spelling`, so one pattern serves a string and a
    number alike and each language's syntax has exactly one owner.
    """
    return re.compile(
        rf"^{re.escape(symbol)}(?:\s*:\s*\w+)?\s*=\s*(?P<v>\"[^\"]*\"|[^\n#]+?)\s*$",
        re.MULTILINE,
    )


def cs_const(symbol: str) -> re.Pattern[str]:
    """A C# `const` field initialiser — `public const int SampleRate = 16_000;`."""
    return re.compile(rf"\bconst\s+\w+\s+{re.escape(symbol)}\s*=\s*(?P<v>[^;]+?)\s*;")


def cs_property(symbol: str) -> re.Pattern[str]:
    """A C# auto-property default — `public TimeSpan DrainBudget { get; init; } = X;`.

    DOTALL because the backoff ladder's collection expression spans six lines;
    a collection expression contains no `;` of its own, so the lazy run to the
    terminating `;` still stops at the right place.
    """
    return re.compile(
        rf"\b{re.escape(symbol)}\s*\{{\s*get;\s*init;\s*\}}\s*=\s*(?P<v>[^;]+?)\s*;",
        re.DOTALL,
    )


#: Regex escapes (`\d`, `\(`, …). Stripped before looking for a word anchor —
#: the `d` in `\d` is regex syntax, not a word the prose actually contains.
_ESCAPE = re.compile(r"\\.")

#: Characters that don't distinguish one number in prose from another.
_NOT_AN_ANCHOR = re.compile(r"[^A-Za-z]")


def anchored(regex: str) -> re.Pattern[str]:
    """A hand-anchored matcher for one value that has no symbol to hang on.

    Prose has no symbols to anchor on, and 320 / 640 / 8 appear as ordinary
    numbers across four docs. So each prose site spells its own matcher — and
    this builder REFUSES one that isn't anchored on surrounding words, which
    is the failure mode that would silently rewrite the wrong number.

    Anchor on the words, never on a sibling contract value: write
    ``samples = (?P<v>\\d+) bytes``, not ``\\(320 samples = (?P<v>\\d+)``, or
    changing the frame size breaks the pattern that was meant to update it.
    """
    if "(?P<v>" not in regex:
        raise ValueError(f"prose matcher must capture the value in a group named 'v': {regex!r}")
    outside = _ESCAPE.sub("", regex.replace("(?P<v>", ""))
    if not _NOT_AN_ANCHOR.sub("", outside):
        raise ValueError(f"prose matcher has no word anchor, so it would match any number: {regex!r}")
    return re.compile(regex)


def js_const(symbol: str) -> re.Pattern[str]:
    """A JS `const` declaration — string, number or array literal."""
    return re.compile(rf"\bconst\s+{re.escape(symbol)}\s*=\s*(?P<v>\[[^\]]*\]|[^;\n]+?)\s*;")


def declared_value(text: str, site: Site) -> Any:
    """The value ``site`` currently declares in ``text``.

    Raises `AnchorNotFound` when the pattern matches nothing — see that class
    for why this is never a soft failure.
    """
    matches = list(site.pattern.finditer(text))
    if not matches:
        raise AnchorNotFound(
            f"{site.path}: no declaration of {site.symbol!r} matched "
            f"(contract key {site.key!r}). It was renamed, reformatted or "
            f"removed — update this site's pattern in tools/stamp_tap_wire.py."
        )
    values = [site.spelling.parse(m.group("v")) for m in matches]
    distinct = [v for i, v in enumerate(values) if v not in values[:i]]
    if len(distinct) > 1:
        raise SiteDisagreesWithItself(
            f"{site.path}: {site.symbol!r} is declared {len(matches)} times with "
            f"{len(distinct)} different values ({distinct!r}) — this file has "
            f"already drifted against itself (contract key {site.key!r})."
        )
    return values[0]


def restamped(text: str, site: Site, value: Any) -> str:
    """`text` with `site`'s declaration rewritten to `value`, in that site's
    own spelling. Only the captured value is replaced, so surrounding
    formatting survives — a whole-file reserialise would re-flow the source
    and swamp the real change in diff noise."""
    if site.spelling.render is None:
        raise NotStampable(
            f"{site.path}: {site.symbol!r} is a gate-only site (contract key "
            f"{site.key!r}) and must not be stamped. The Blip-resilience recipe "
            f"is pinned by the golden table in tests/test_tap_wire_contract.py, "
            f"not written from the Recorder — see ADR-0019."
        )
    matches = list(site.pattern.finditer(text))
    if not matches:
        raise AnchorNotFound(
            f"{site.path}: no declaration of {site.symbol!r} matched "
            f"(contract key {site.key!r}) — nothing to stamp."
        )
    rendered = site.spelling.render(value)
    # Right to left, so an earlier replacement can't shift a later span.
    out = text
    for m in reversed(matches):
        start, end = m.span("v")
        out = out[:start] + rendered + out[end:]
    return out


# ---------------------------------------------------------------------------
# The Recorder — the source
# ---------------------------------------------------------------------------


def recorder_contract() -> dict[str, Any]:
    """The wire contract, read from the Recorder's own constants.

    Imported, never copied: the Recorder serves `/tap`, so whatever these say
    IS the contract, and a gate holding its own copy could only ever drift
    from the thing it is checking.

    The values are spread across five modules because each is cohesive where
    it sits (`speech_gate` aliases `audio.RECORDER_SAMPLE_RATE` exactly as
    `strip_silence` and `wav_predecode` do). This function is the only place
    that needs to know they jointly form one contract — which is why there is
    no `tapscribe/tap_wire.py`.
    """
    # Importable when run as `python3 tools/stamp_tap_wire.py` from anywhere,
    # not just under pytest (whose conftest already puts the root on the path).
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from tapscribe import auth, config, speech_gate, tap_fan_out
    from tapscribe.audio import RECORDER_CHANNELS, RECORDER_SAMPLE_RATE, RECORDER_SAMPLE_WIDTH

    return {
        "subprotocol_prefix": auth.TAP_SUBPROTOCOL_PREFIX,
        "sample_rate": RECORDER_SAMPLE_RATE,
        "channels": RECORDER_CHANNELS,
        "sample_width": RECORDER_SAMPLE_WIDTH,
        "frame_samples": speech_gate.FRAME_SAMPLES,
        "frame_bytes": speech_gate.FRAME_BYTES,
        "probe_identity": tap_fan_out.PROBE_IDENTITY,
        "tap_prefix": config.TAP_PREFIX,
        # Derived, and that is exactly why it needs stamping. CODE derives
        # the frame duration where it needs it; PROSE states "20 ms"
        # outright, five times across four docs. A frame-size change would
        # leave every one of them quietly wrong.
        "frame_ms": speech_gate.FRAME_SAMPLES * 1000 // RECORDER_SAMPLE_RATE,
    }


# ---------------------------------------------------------------------------
# Tier 1 — the Wire contract. Stamped FROM the Recorder.
# ---------------------------------------------------------------------------

_SC = Path("bridges/spacialchat-bridge")
_TRAY = Path("bridges/windows-tray-bridge/src/TapScribe.Bridge.Core")

STAMPS: tuple[Site, ...] = (
    # --- C#: the tray bridge's wire constants ------------------------------
    Site(_TRAY / "TapWire.cs", "sample_rate", "SampleRate", cs_const("SampleRate"), CS_INT),
    Site(_TRAY / "TapWire.cs", "channels", "Channels", cs_const("Channels"), CS_INT),
    Site(_TRAY / "TapWire.cs", "frame_samples", "FrameSamples", cs_const("FrameSamples"), CS_INT),
    Site(
        _TRAY / "TapWire.cs",
        "subprotocol_prefix",
        "SubprotocolPrefix",
        cs_const("SubprotocolPrefix"),
        TEXT,
    ),
    # `FrameBytes = FrameSamples * 2` is DERIVED — never stamped.
    # The XML doc comment spells the offered subprotocol out for IntelliSense;
    # found by the completeness tripwire, which is exactly its job.
    Site(
        _TRAY / "TapClient.cs",
        "subprotocol_prefix",
        "`tapscribe.vN.tap.&lt;token&gt;` in the XML doc",
        anchored(r"`(?P<v>tapscribe\.v\d+\.tap\.)&lt;token&gt;`"),
        RAW,
    ),
    Site(
        _TRAY / "ConnectionTester.cs",
        "probe_identity",
        "Identity",
        anchored(r"Identity = (?P<v>\"[^\"]*\")"),
        TEXT,
    ),
    # --- JS: the SpatialChat bridge, both worlds ---------------------------
    Site(
        _SC / "control-client.js",
        "subprotocol_prefix",
        "TAP_SUBPROTOCOL_PREFIX",
        js_const("TAP_SUBPROTOCOL_PREFIX"),
        TEXT,
    ),
    Site(
        _SC / "control-client.js",
        "probe_identity",
        "identity= in the probe URL",
        anchored(r"/tap\?identity=(?P<v>[^&\"]+)&name=probe"),
        RAW,
    ),
    Site(
        _SC / "page-script.js",
        "frame_samples",
        "FRAME_SAMPLES",
        js_const("FRAME_SAMPLES"),
        INT,
    ),
    # --- Python: the local-test bridge -------------------------------------
    Site(
        Path("bridges/local-test-bridge/local_test_bridge.py"),
        "sample_rate",
        "SAMPLE_RATE",
        py_assign("SAMPLE_RATE"),
        INT,
    ),
    Site(
        Path("bridges/local-test-bridge/local_test_bridge.py"),
        "frame_samples",
        "FRAME_SAMPLES",
        py_assign("FRAME_SAMPLES"),
        INT,
    ),
    Site(
        Path("bridges/local-test-bridge/local_test_bridge.py"),
        "subprotocol_prefix",
        "TAP_SUBPROTOCOL_PREFIX",
        py_assign("TAP_SUBPROTOCOL_PREFIX"),
        TEXT,
    ),
    # --- Prose. Each pattern rewrites EVERY occurrence in its file. --------
    Site(
        Path("bridges/README.md"),
        "sample_rate",
        "16 kHz mono",
        anchored(r"(?P<v>\d+) kHz mono"),
        KHZ,
    ),
    Site(
        Path("bridges/README.md"),
        "frame_ms",
        "N ms",
        anchored(r"(?P<v>\d+) ms \("),
        INT,
    ),
    Site(
        Path("bridges/spacialchat-bridge/README.md"),
        "frame_ms",
        "N ms",
        anchored(r"in (?P<v>\d+) ms \("),
        INT,
    ),
    Site(
        Path("bridges/windows-tray-bridge/README.md"),
        "frame_ms",
        "N ms",
        # Both mentions in this file are the frame duration, so a plain "N ms"
        # anchor is safe here — unlike bridges/README.md, where `5000 ms` and
        # `8000 ms` (the Blip-resilience recipe) would also match.
        anchored(r"(?P<v>\d+) ms"),
        INT,
    ),
    Site(
        Path("CONTEXT.md"),
        "frame_ms",
        "N ms / N bytes per frame",
        anchored(r"\((?P<v>\d+) ms / \d+ bytes per frame\)"),
        INT,
    ),
    Site(
        Path("bridges/README.md"),
        "frame_samples",
        "(N samples = ...)",
        anchored(r"\((?P<v>\d+) samples ="),
        INT,
    ),
    Site(
        Path("bridges/README.md"),
        "frame_bytes",
        "(... = N bytes)",
        anchored(r"samples = (?P<v>\d+) bytes\)"),
        INT,
    ),
    Site(
        Path("bridges/README.md"),
        "subprotocol_prefix",
        "`tapscribe.vN.tap.<token>`",
        anchored(r"`(?P<v>tapscribe\.v\d+\.tap\.)<token>`"),
        RAW,
    ),
    Site(
        Path("bridges/README.md"),
        "probe_identity",
        "`__probe__` is RESERVED",
        anchored(r"\*\*`(?P<v>__probe__)` is RESERVED\*\*"),
        RAW,
    ),
    Site(
        Path("bridges/windows-tray-bridge/README.md"),
        "sample_rate",
        "16 kHz mono",
        anchored(r"(?P<v>\d+) kHz mono"),
        KHZ,
    ),
    Site(
        Path("bridges/windows-tray-bridge/README.md"),
        "frame_bytes",
        "N-byte",
        anchored(r"(?P<v>\d+)-byte"),
        INT,
    ),
    Site(
        Path("bridges/windows-tray-bridge/README.md"),
        "subprotocol_prefix",
        "`tapscribe.vN.tap.<token>`",
        anchored(r"`(?P<v>tapscribe\.v\d+\.tap\.)<token>`"),
        RAW,
    ),
    Site(
        _SC / "README.md",
        "sample_rate",
        "16 kHz mono",
        anchored(r"(?P<v>\d+) kHz mono"),
        KHZ,
    ),
    Site(
        _SC / "README.md",
        "frame_bytes",
        "N-byte",
        anchored(r"(?P<v>\d+)-byte"),
        INT,
    ),
    Site(
        Path("CONTEXT.md"),
        "sample_rate",
        "16 kHz mono",
        anchored(r"(?P<v>\d+) kHz mono"),
        KHZ,
    ),
    Site(
        Path("CONTEXT.md"),
        "frame_bytes",
        "N bytes per frame",
        anchored(r"(?P<v>\d+) bytes per frame"),
        INT,
    ),
)


# ---------------------------------------------------------------------------
# Tier 2 — the Blip-resilience recipe. GATED, never stamped.
# ---------------------------------------------------------------------------
#
# The Recorder has no opinion on these, so there is nothing to stamp FROM: a
# third-party Bridge may deviate (CONTEXT.md, "Blip-resilience recipe"). What
# must not happen is the two BUNDLED bridges and the docs drifting from each
# other, which is what the gate's golden table pins. `local-test-bridge` has
# no reconnect ladder at all and is exempt by construction.

RECIPE: tuple[Site, ...] = (
    Site(
        _SC / "content.js",
        "backoff_ms",
        "BACKOFF_MS",
        js_const("BACKOFF_MS"),
        readonly(INT_LIST),
    ),
    Site(
        _SC / "content.js",
        "backoff_cap_ms",
        "BACKOFF_CAP_MS",
        js_const("BACKOFF_CAP_MS"),
        readonly(INT),
    ),
    Site(
        _SC / "content.js",
        "max_buffer_bytes",
        "MAX_BUFFER_BYTES",
        js_const("MAX_BUFFER_BYTES"),
        readonly(INT),
    ),
    Site(
        _SC / "content.js",
        "drain_budget_ms",
        "DRAIN_MAX_MS",
        js_const("DRAIN_MAX_MS"),
        readonly(INT),
    ),
    # content.js spells the jitter as the WIDTH of a symmetric span
    # (`(Math.random() - 0.5) * 0.5`), C# as the fraction either side (0.25).
    # Same fact, half the number — hence the /2 in the spelling.
    Site(
        _SC / "content.js",
        "backoff_jitter",
        "(Math.random() - 0.5) * N",
        anchored(r"\(Math\.random\(\) - 0\.5\) \* (?P<v>[\d.]+)"),
        Spelling(parse=lambda s: float(s) / 2),
    ),
    Site(
        _TRAY / "TapStreamOptions.cs",
        "backoff_ms",
        "Backoff",
        cs_property("Backoff"),
        CS_TIMESPAN_LIST_MS,
    ),
    Site(
        _TRAY / "TapStreamOptions.cs",
        "backoff_cap_ms",
        "BackoffCap",
        cs_property("BackoffCap"),
        CS_TIMESPAN_MS,
    ),
    Site(
        _TRAY / "TapStreamOptions.cs",
        "backoff_jitter",
        "BackoffJitter",
        cs_property("BackoffJitter"),
        readonly(FLOAT),
    ),
    Site(
        _TRAY / "TapStreamOptions.cs",
        "max_buffer_bytes",
        "MaxBufferBytes",
        cs_property("MaxBufferBytes"),
        readonly(CS_INT),
    ),
    Site(
        _TRAY / "TapStreamOptions.cs",
        "drain_budget_ms",
        "DrainBudget",
        cs_property("DrainBudget"),
        CS_TIMESPAN_MS,
    ),
    Site(
        Path("bridges/README.md"),
        "backoff_ms",
        "jittered exponential — `... ms`",
        anchored(r"jittered exponential — `(?P<v>[\d, ]+) ms`"),
        readonly(INT_LIST),
    ),
    Site(
        Path("bridges/README.md"),
        "backoff_cap_ms",
        "capped at `N ms`",
        anchored(r"capped at `(?P<v>\d+) ms`"),
        readonly(INT),
    ),
    Site(
        Path("bridges/README.md"),
        "backoff_jitter",
        "**±N % jitter**",
        anchored(r"\*\*±(?P<v>[\d.]+) % jitter\*\*"),
        PERCENT,
    ),
    Site(
        Path("bridges/README.md"),
        "max_buffer_bytes",
        "**N bytes**",
        anchored(r"\*\*(?P<v>[\d ]+) bytes\*\*"),
        readonly(SPACED_INT),
    ),
    Site(
        Path("bridges/README.md"),
        "drain_budget_ms",
        "recommended **N ms**",
        anchored(r"recommended \*\*(?P<v>\d+) ms\*\*"),
        readonly(INT),
    ),
    Site(
        Path("CONTEXT.md"),
        "drain_budget_ms",
        "`DRAIN_MAX_MS` (N s)",
        anchored(r"`DRAIN_MAX_MS` \((?P<v>\d+) s\)"),
        readonly(SECONDS_MS),
    ),
)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def stamp(root: Path | None = None) -> list[Path]:
    """Bring every stamped site into line with the Recorder.

    Returns the paths actually rewritten — empty when the tree is already
    consistent, which is the normal case and the reason re-running is free.
    `root` lets the tests drive a throwaway copy instead of the worktree.
    """
    base = REPO_ROOT if root is None else root
    contract = recorder_contract()

    edits: dict[Path, str] = {}
    for site in STAMPS:
        text = edits.get(site.path)
        if text is None:
            text = (base / site.path).read_text(encoding="utf-8")
        edits[site.path] = restamped(text, site, contract[site.key])

    changed: list[Path] = []
    for path, text in edits.items():
        target = base / path
        if target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8")
            changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Stamp the /tap wire constants from the Recorder into every Bridge. "
            "Edit tapscribe/ first, then run this."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit 1 without writing (what CI's gate does).",
    )
    args = parser.parse_args(argv)

    if args.check:
        stale = [
            site
            for site in STAMPS
            if declared_value((REPO_ROOT / site.path).read_text(encoding="utf-8"), site)
            != recorder_contract()[site.key]
        ]
        for site in stale:
            print(f"drifted: {site.path} ({site.symbol})")
        return 1 if stale else 0

    changed = stamp()
    for path in changed:
        print(f"stamped: {path}")
    print(f"{len(changed)} file(s) changed" if changed else "already consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
