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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

#: The module's public surface. `tests/test_tap_wire_contract.py` is the only
#: consumer, and it reaches these through `stamper.<name>` — an access CodeQL
#: cannot resolve, since `tools/` is not an importable package from its point
#: of view, so without this the tables read as dead globals. Stating the
#: exports is what a reader wants anyway.
__all__ = [
    "RECIPE",
    "STAMPS",
    "Anchor",
    "AnchorNotFound",
    "NotStampable",
    "Site",
    "SiteDisagreesWithItself",
    "Spelling",
    "anchored",
    "cs_const",
    "cs_property",
    "declared_value",
    "js_const",
    "py_assign",
    "recorder_contract",
    "restamped",
    "stamp",
]

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


def _to_thousands(value: int) -> str:
    """2000 -> "2". Refuses a value that isn't a whole number of thousands.

    Without the guard this floor-divides: a 44 100 Hz wire would be written
    into four docs as "44 kHz", silently wrong — a stamper that quietly
    corrupts prose is worse than the drift it exists to prevent. Same class of
    bug as the sample-width spelling, which the anti-vacuity sweep caught.
    """
    if value % 1000:
        raise ValueError(
            f"{value} is not a whole number of thousands, so it cannot be spelled "
            f"in k-units; this site's prose needs a different anchor."
        )
    return str(value // 1000)


#: Prose that states the value in thousands of the canonical unit — "16 kHz
#: mono" (Hz), "`DRAIN_MAX_MS` (8 s)" (ms). ONE spelling: the arithmetic is
#: identical either way, and the unit is already named by each site's anchor.
THOUSANDS = Spelling(parse=lambda s: int(s) * 1000, render=_to_thousands)

#: Prose that spaces its thousands — "**96 000 bytes**". Gate-only, so the
#: render it never reaches is simply absent.
SPACED_INT = Spelling(parse=lambda s: int(s.replace(" ", "")))


def _bits_to_bytes(declared: str) -> int:
    """ "16" -> 2. Rejects a width that isn't whole bytes: without that, the
    anti-vacuity sweep showed "signed 17-bit" floor-dividing to the same 2 and
    the site proving nothing."""
    bits = int(declared)
    if bits % 8:
        raise ValueError(f"sample width must be a whole number of bytes, got {bits} bits")
    return bits // 8


#: Prose that states the sample width in bits — "PCM signed 16-bit".
BITS = Spelling(parse=_bits_to_bytes, render=lambda v: str(v * 8))

#: Prose that states a fraction as a percentage — "**±25 % jitter**".
PERCENT = Spelling(parse=lambda s: float(s) / 100)

#: A JS array literal of integers. Read-only: tier 2.
INT_LIST = Spelling(parse=lambda s: [int(p) for p in re.findall(r"-?\d+", s)])

#: A bare float — `public double BackoffJitter { get; init; } = 0.25;`.
FLOAT = Spelling(parse=float)

#: A value embedded in a larger literal (a URL query string), so it carries no
#: quotes of its own — `"/tap?identity=__probe__&name=probe"`.
RAW = Spelling(parse=str.strip, render=str)


def _gate_only(*sites: Site) -> tuple[Site, ...]:
    """Strip every render from a whole tier: gated, never stamped.

    `stamp()` iterates `STAMPS` alone, so the table split is already the
    structural guarantee. This is the second lock, and it is applied to the
    TIER rather than to each row — a row added to `RECIPE` later inherits it
    instead of depending on the author remembering a per-row wrapper. (It was
    per-row once: 8 of 16 rows carried it, three of those were no-ops, and a
    reader could not tell which rows were locked by policy and which merely
    by a spelling that happened to lack a render.)
    """
    return tuple(replace(s, spelling=Spelling(parse=s.spelling.parse)) for s in sites)


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
class Anchor:
    """A matcher plus what the thing it matches is CALLED in that file.

    The two travel together because a `Site` used to name its symbol twice —
    once as text and once inside the builder call — which no test could catch
    when they disagreed, and which made every row multi-line.

    ``name`` is set only for a real identifier (`py_assign` & friends), and is
    what the completeness sweep derives its watch-list from; prose anchors have
    no identifier, so they carry ``None`` and a human-readable ``label``.
    """

    label: str
    pattern: re.Pattern[str]
    name: str | None = None


@dataclass(frozen=True)
class Site:
    """One place that declares one contract value.

    The anchor's pattern must capture the value in a group named ``v``,
    anchored on the symbol or markup around it so it can't match a
    coincidental number.
    """

    path: Path
    key: str
    anchor: Anchor
    spelling: Spelling = TEXT

    @property
    def symbol(self) -> str:
        """What to call this declaration in an error message."""
        return self.anchor.label

    @property
    def pattern(self) -> re.Pattern[str]:
        return self.anchor.pattern


def py_assign(symbol: str) -> Anchor:
    """A module-level Python assignment — `SAMPLE_RATE = 16000`.

    Every pattern here captures the RAW right-hand side; unquoting and unit
    conversion belong to the `Spelling`, so one pattern serves a string and a
    number alike and each language's syntax has exactly one owner.
    """
    return Anchor(
        symbol,
        re.compile(
            rf"^{re.escape(symbol)}(?:\s*:\s*\w+)?\s*=\s*(?P<v>\"[^\"]*\"|[^\n#]+?)\s*$",
            re.MULTILINE,
        ),
        name=symbol,
    )


def cs_const(symbol: str) -> Anchor:
    """A C# `const` field initialiser — `public const int SampleRate = 16_000;`."""
    return Anchor(
        symbol,
        re.compile(rf"\bconst\s+\w+\s+{re.escape(symbol)}\s*=\s*(?P<v>[^;]+?)\s*;"),
        name=symbol,
    )


def cs_property(symbol: str) -> Anchor:
    """A C# auto-property default — `public TimeSpan DrainBudget { get; init; } = X;`.

    DOTALL because the backoff ladder's collection expression spans six lines;
    a collection expression contains no `;` of its own, so the lazy run to the
    terminating `;` still stops at the right place.
    """
    return Anchor(
        symbol,
        re.compile(
            rf"\b{re.escape(symbol)}\s*\{{\s*get;\s*init;\s*\}}\s*=\s*(?P<v>[^;]+?)\s*;",
            re.DOTALL,
        ),
        name=symbol,
    )


#: Regex escapes (`\d`, `\(`, …). Stripped before looking for a word anchor —
#: the `d` in `\d` is regex syntax, not a word the prose actually contains.
_ESCAPE = re.compile(r"\\.")

#: Characters that don't distinguish one number in prose from another.
_NOT_AN_ANCHOR = re.compile(r"[^A-Za-z]")


def anchored(regex: str, *, called: str) -> Anchor:
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
    # `name` stays None: prose declares no identifier, so there is nothing for
    # the completeness sweep's watch-list to derive from here — a restatement
    # in prose is sweep B's job, not sweep A's.
    return Anchor(called, re.compile(regex))


def js_const(symbol: str) -> Anchor:
    """A JS `const` declaration — string, number or array literal."""
    return Anchor(
        symbol,
        re.compile(rf"\bconst\s+{re.escape(symbol)}\s*=\s*(?P<v>\[[^\]]*\]|[^;\n]+?)\s*;"),
        name=symbol,
    )


def _matches(text: str, site: Site) -> list[re.Match[str]]:
    """Every occurrence `site` declares in `text`, or raise.

    Raising rather than returning an empty list is the fail-CLOSED half of the
    gate: `None`/empty would compare equal across two un-anchored sites and
    turn the whole thing green while checking nothing.
    """
    matches = list(site.pattern.finditer(text))
    if not matches:
        raise AnchorNotFound(
            f"{site.path}: no declaration of {site.symbol!r} matched "
            f"(contract key {site.key!r}). It was renamed, reformatted or "
            f"removed — update this site's pattern in tools/stamp_tap_wire.py."
        )
    return matches


def declared_value(text: str, site: Site) -> Any:
    """The value ``site`` currently declares in ``text``.

    Raises `AnchorNotFound` when the pattern matches nothing — see that class
    for why this is never a soft failure.
    """
    matches = _matches(text, site)
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
    matches = _matches(text, site)
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


def _frame_ms(frame_samples: int, sample_rate: int) -> int:
    """Frame duration in whole milliseconds, or raise.

    Derived, and stamped only into PROSE (code computes it where it needs it).
    Integer division would silently call a 321-sample frame "20 ms" and write
    that into four docs, so a frame that isn't a whole number of milliseconds
    is an error rather than a rounding.
    """
    if (frame_samples * 1000) % sample_rate:
        raise ValueError(
            f"{frame_samples} samples at {sample_rate} Hz is not a whole number of "
            f"milliseconds, so the docs' 'N ms' phrasing can no longer be stamped."
        )
    return frame_samples * 1000 // sample_rate


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

    from tapscribe import auth, speech_gate, tap_fan_out, tap_mode
    from tapscribe.audio import RECORDER_CHANNELS, RECORDER_SAMPLE_RATE, RECORDER_SAMPLE_WIDTH

    return {
        "subprotocol_prefix": auth.TAP_SUBPROTOCOL_PREFIX,
        "sample_rate": RECORDER_SAMPLE_RATE,
        "channels": RECORDER_CHANNELS,
        "sample_width": RECORDER_SAMPLE_WIDTH,
        "frame_samples": speech_gate.FRAME_SAMPLES,
        "frame_bytes": speech_gate.FRAME_BYTES,
        "probe_identity": tap_fan_out.PROBE_IDENTITY,
        # Reserved `tap_mode` spellings. Two keys, not one: no Spelling writes a
        # list, and a file declaring one key twice raises SiteDisagreesWithItself.
        "tap_mode_single": tap_mode.TAP_MODE_SINGLE,
        "tap_mode_multi": tap_mode.TAP_MODE_MULTI,
        # Derived, and that is exactly why it needs stamping. CODE derives
        # the frame duration where it needs it; PROSE states "20 ms"
        # outright, five times across four docs. A frame-size change would
        # leave every one of them quietly wrong.
        "frame_ms": _frame_ms(speech_gate.FRAME_SAMPLES, RECORDER_SAMPLE_RATE),
    }


# ---------------------------------------------------------------------------
# Tier 1 — the Wire contract. Stamped FROM the Recorder.
# ---------------------------------------------------------------------------

_SC = Path("bridges/spacialchat-bridge")
_TRAY = Path("bridges/tray-bridge/src/TapScribe.Bridge.Core")
_LTB = Path("bridges/local-test-bridge/local_test_bridge.py")
_BRIDGES_README = Path("bridges/README.md")
_TRAY_README = Path("bridges/tray-bridge/README.md")
_SC_README = _SC / "README.md"
_CONTEXT = Path("CONTEXT.md")

#: Prose restatements that appear verbatim in more than one doc. Each path is
#: still listed literally and still gets its own row (and its own failure id);
#: this only stops the same anchor being retyped once per file.
_KHZ = anchored(r"(?P<v>\d+) kHz mono", called="16 kHz mono")
_NBYTE = anchored(r"(?P<v>\d+)-byte", called="N-byte")
_SUBPROTO_DOC = anchored(r"`(?P<v>tapscribe\.v\d+\.tap\.)<token>`", called="`tapscribe.vN.tap.<token>`")


def _in_each(paths: tuple[Path, ...], key: str, anchor: Anchor, spelling: Spelling) -> tuple[Site, ...]:
    """One restatement of `key`, appearing in several files."""
    return tuple(Site(p, key, anchor, spelling) for p in paths)


STAMPS: tuple[Site, ...] = (
    # --- C#: the tray bridge -----------------------------------------------
    Site(_TRAY / "TapWire.cs", "sample_rate", cs_const("SampleRate"), CS_INT),
    Site(_TRAY / "TapWire.cs", "channels", cs_const("Channels"), CS_INT),
    Site(_TRAY / "TapWire.cs", "frame_samples", cs_const("FrameSamples"), CS_INT),
    Site(_TRAY / "TapWire.cs", "subprotocol_prefix", cs_const("SubprotocolPrefix"), TEXT),
    # `FrameBytes = FrameSamples * 2` is DERIVED — never stamped.
    Site(
        _TRAY / "TapClient.cs",
        "subprotocol_prefix",
        anchored(
            r"`(?P<v>tapscribe\.v\d+\.tap\.)&lt;token&gt;`",
            called="`tapscribe.vN.tap.<token>` in the XML doc",
        ),
        RAW,
    ),
    Site(
        _TRAY / "ConnectionTester.cs",
        "probe_identity",
        anchored(r"Identity = (?P<v>\"[^\"]*\")", called="Identity"),
        TEXT,
    ),
    # --- tap_mode: the single/multi declaration (ADR-0021) ------------------
    Site(_TRAY / "TapConnectionOptions.cs", "tap_mode_single", cs_const("TapModeSingle"), TEXT),
    Site(_TRAY / "TapConnectionOptions.cs", "tap_mode_multi", cs_const("TapModeMulti"), TEXT),
    Site(_LTB, "tap_mode_single", py_assign("TAP_MODE_SINGLE"), TEXT),
    Site(_SC / "content.js", "tap_mode_single", js_const("TAP_MODE_SINGLE"), TEXT),
    Site(
        _BRIDGES_README,
        "tap_mode_single",
        anchored(r"Exactly \*\*`(?P<v>[a-z]+)`\*\* or", called="`single` in the tap_mode bullet"),
        RAW,
    ),
    Site(
        _BRIDGES_README,
        "tap_mode_multi",
        anchored(r"or \*\*`(?P<v>[a-z]+)`\*\*; absent", called="`multi` in the tap_mode bullet"),
        RAW,
    ),
    # --- JS: the SpatialChat bridge, both worlds ---------------------------
    Site(_SC / "control-client.js", "subprotocol_prefix", js_const("TAP_SUBPROTOCOL_PREFIX"), TEXT),
    Site(
        _SC / "control-client.js",
        "probe_identity",
        anchored(r"/tap\?identity=(?P<v>[^&\"]+)&name=probe", called="identity= in the probe URL"),
        RAW,
    ),
    Site(_SC / "page-script.js", "frame_samples", js_const("FRAME_SAMPLES"), INT),
    # content.js restates the whole wire contract in its header block. It is
    # shipped bridge code under a "Wire contract" heading, not incidental
    # prose, so it is stamped like any other declaration (found by the
    # completeness review, not by the first cut of the table).
    Site(_SC / "content.js", "sample_rate", _KHZ, THOUSANDS),
    Site(_SC / "content.js", "frame_ms", anchored(r"(?P<v>\d+) ms each", called="N ms each"), INT),
    Site(
        _SC / "content.js",
        "frame_samples",
        anchored(r"each \((?P<v>\d+) samples", called="(N samples ...)"),
        INT,
    ),
    Site(
        _SC / "content.js",
        "frame_bytes",
        anchored(r"samples = (?P<v>\d+) bytes\)", called="(... = N bytes)"),
        INT,
    ),
    # --- Python: the local-test bridge -------------------------------------
    Site(_LTB, "sample_rate", py_assign("SAMPLE_RATE"), INT),
    Site(_LTB, "frame_samples", py_assign("FRAME_SAMPLES"), INT),
    Site(_LTB, "subprotocol_prefix", py_assign("TAP_SUBPROTOCOL_PREFIX"), TEXT),
    # --- Prose. Each pattern rewrites EVERY occurrence in its file. --------
    *_in_each((_BRIDGES_README, _TRAY_README, _SC_README, _CONTEXT), "sample_rate", _KHZ, THOUSANDS),
    *_in_each((_TRAY_README, _SC_README), "frame_bytes", _NBYTE, INT),
    *_in_each((_BRIDGES_README, _TRAY_README), "subprotocol_prefix", _SUBPROTO_DOC, RAW),
    Site(_BRIDGES_README, "sample_width", anchored(r"signed (?P<v>\d+)-bit", called="signed N-bit"), BITS),
    Site(_BRIDGES_README, "frame_ms", anchored(r"(?P<v>\d+) ms \(", called="N ms ("), INT),
    Site(_BRIDGES_README, "frame_samples", anchored(r"\((?P<v>\d+) samples =", called="(N samples ="), INT),
    Site(
        _BRIDGES_README,
        "frame_bytes",
        anchored(r"samples = (?P<v>\d+) bytes\)", called="(... = N bytes)"),
        INT,
    ),
    Site(
        _BRIDGES_README,
        "probe_identity",
        anchored(r"\*\*`(?P<v>__probe__)` is RESERVED\*\*", called="`__probe__` is RESERVED"),
        RAW,
    ),
    # Both "N ms" mentions in the tray README are the frame duration, so a
    # plain anchor is safe there — unlike bridges/README.md, where the
    # Blip-resilience recipe's `5000 ms` / `8000 ms` would also match.
    Site(_TRAY_README, "frame_ms", anchored(r"(?P<v>\d+) ms", called="N ms"), INT),
    Site(_SC_README, "frame_ms", anchored(r"in (?P<v>\d+) ms \(", called="in N ms ("), INT),
    Site(
        _CONTEXT,
        "frame_ms",
        anchored(r"\((?P<v>\d+) ms / \d+ bytes per frame\)", called="(N ms / N bytes per frame)"),
        INT,
    ),
    Site(_CONTEXT, "frame_bytes", anchored(r"(?P<v>\d+) bytes per frame", called="N bytes per frame"), INT),
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

RECIPE: tuple[Site, ...] = _gate_only(
    Site(_SC / "content.js", "backoff_ms", js_const("BACKOFF_MS"), INT_LIST),
    Site(_SC / "content.js", "backoff_cap_ms", js_const("BACKOFF_CAP_MS"), INT),
    Site(_SC / "content.js", "max_buffer_bytes", js_const("MAX_BUFFER_BYTES"), INT),
    Site(_SC / "content.js", "drain_budget_ms", js_const("DRAIN_MAX_MS"), INT),
    # content.js spells the jitter as the WIDTH of a symmetric span
    # (`(Math.random() - 0.5) * 0.5`), C# as the fraction either side (0.25).
    # Same fact, half the number — hence the /2 in the spelling.
    Site(
        _SC / "content.js",
        "backoff_jitter",
        anchored(r"\(Math\.random\(\) - 0\.5\) \* (?P<v>[\d.]+)", called="(Math.random() - 0.5) * N"),
        Spelling(parse=lambda s: float(s) / 2),
    ),
    Site(_TRAY / "TapStreamOptions.cs", "backoff_ms", cs_property("Backoff"), CS_TIMESPAN_LIST_MS),
    Site(_TRAY / "TapStreamOptions.cs", "backoff_cap_ms", cs_property("BackoffCap"), CS_TIMESPAN_MS),
    Site(_TRAY / "TapStreamOptions.cs", "backoff_jitter", cs_property("BackoffJitter"), FLOAT),
    Site(_TRAY / "TapStreamOptions.cs", "max_buffer_bytes", cs_property("MaxBufferBytes"), CS_INT),
    Site(_TRAY / "TapStreamOptions.cs", "drain_budget_ms", cs_property("DrainBudget"), CS_TIMESPAN_MS),
    Site(
        _BRIDGES_README,
        "backoff_ms",
        anchored(r"jittered exponential — `(?P<v>[\d, ]+) ms`", called="jittered exponential — `... ms`"),
        INT_LIST,
    ),
    Site(
        _BRIDGES_README,
        "backoff_cap_ms",
        anchored(r"capped at `(?P<v>\d+) ms`", called="capped at `N ms`"),
        INT,
    ),
    Site(
        _BRIDGES_README,
        "backoff_jitter",
        anchored(r"\*\*±(?P<v>[\d.]+) % jitter\*\*", called="**±N % jitter**"),
        PERCENT,
    ),
    Site(
        _BRIDGES_README,
        "max_buffer_bytes",
        anchored(r"\*\*(?P<v>[\d ]+) bytes\*\*", called="**N bytes**"),
        SPACED_INT,
    ),
    Site(
        _BRIDGES_README,
        "drain_budget_ms",
        anchored(r"recommended \*\*(?P<v>\d+) ms\*\*", called="recommended **N ms**"),
        INT,
    ),
    Site(
        _CONTEXT,
        "drain_budget_ms",
        anchored(r"`DRAIN_MAX_MS` \((?P<v>\d+) s\)", called="`DRAIN_MAX_MS` (N s)"),
        THOUSANDS,
    ),
)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def stamp(root: Path = REPO_ROOT) -> list[Path]:
    """Bring every stamped site into line with the Recorder.

    Returns the paths actually rewritten — empty when the tree is already
    consistent, which is the normal case and the reason re-running is free.
    `root` lets the tests drive a throwaway copy instead of the worktree.
    """
    contract = recorder_contract()

    original: dict[Path, str] = {}
    edited: dict[Path, str] = {}
    for site in STAMPS:
        if site.path not in original:
            original[site.path] = (root / site.path).read_text(encoding="utf-8")
            edited[site.path] = original[site.path]
        edited[site.path] = restamped(edited[site.path], site, contract[site.key])

    changed: list[Path] = []
    for path, text in edited.items():
        if text != original[path]:
            (root / path).write_text(text, encoding="utf-8")
            changed.append(path)
    return changed


def main() -> int:
    """Stamp, and report what moved.

    There is deliberately no `--check` mode: the gate is
    `tests/test_tap_wire_contract.py`, and a second copy of that predicate here
    could only drift from it. To check by hand, run this (it is idempotent)
    and look at `git diff` — same as `tools/bump_version.py`.
    """
    changed = stamp()
    for path in changed:
        print(f"stamped: {path}")
    print(f"{len(changed)} file(s) changed" if changed else "already consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
