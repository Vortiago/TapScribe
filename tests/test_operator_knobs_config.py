"""RED contract for #210 — the REMAINING operator knobs become dashboard-tunable.

`TAPSCRIBE_MODEL_IDLE_TTL_S` already took this shape in PR #347 (env > file >
default, read at use-time, `tests/test_idle_ttl_config.py`). Four knobs did NOT and
are still env-only, so retuning any of them means restarting the server with an env
var — the one config channel the product philosophy rejects:

  key                    env var                          bounds            default
  parakeet-chunk-s       TAPSCRIBE_PARAKEET_CHUNK_S       (1.0, 600.0)      120.0
  parakeet-overlap-s     TAPSCRIBE_PARAKEET_OVERLAP_S     (0.0, 60.0)        15.0
  summarize-timeout-s    TAPSCRIBE_SUMMARIZE_TIMEOUT_S    (1.0, 3600.0)     120.0
  summarize-gguf-ctx     TAPSCRIBE_SUMMARIZE_GGUF_CTX     (512, 131072)      8192

Each gets the SAME rung idle-TTL has: a dashboard-writable config file under
CONFIG_DIR (`<key>.txt`), read at USE-time, with the knob's existing bounds applying
to the file value exactly as they do to the env value.

Resolution precedence (pinned per knob):

    env var (set AND valid)  >  config file (set AND valid)  >  default

"set AND valid" is the load-bearing half. A set-but-INVALID env var — empty,
non-numeric, out of bounds, non-finite — is NOT a real override and must FALL
THROUGH to the config file. The concrete trigger: a systemd EnvironmentFile leaves
`TAPSCRIBE_PARAKEET_CHUNK_S=` in the environment while the operator sets the value
in the dashboard; keying the env branch on mere presence silently discards what the
operator actually chose.

The tests write the config file DIRECTLY (the location the resolver must read), so
RED shows as clean assertion failures rather than a missing-config-key error.

`repoint_config_files` is self-registering for any new `CONFIG_DIR`-rooted `*_FILE`
constant, so no conftest edit is needed here.

Related, deliberately NOT given a write path (see `test_specialist_table_*`): the
`TAPSCRIBE_SPECIALIST_<LANG>` map is read ONCE AT IMPORT into
`transcribers.catalog.SPECIALIST_MODELS` — a launch-time knob. #210 asks only that
it be VISIBLE ("visibility beats editability here"), so it is surfaced read-only on
/api/state and must NOT become use-time-read; existing tests monkeypatch
`SPECIALIST_MODELS` by `setitem` and that contract stays intact.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from conftest import repoint_config_files  # type: ignore[import-not-found]  # tests/ on sys.path
from wav_builders import seed_wav  # type: ignore[import-not-found]  # tests/ on sys.path

from tapscribe import config
from tapscribe.config_store import read_config, write_config
from tapscribe.diarizers.standalone import resolve_max_speakers, resolve_threshold
from tapscribe.summarizers.catalog import default_gguf_ctx
from tapscribe.summarizers.command import _default_timeout_s
from tapscribe.transcribers._chunked import ChunkedTranscriber

# --------------------------------------------------------------------------------
# The knob table: every dashboard-tunable numeric knob. Each uniform case below
# is parametrized over ALL rows — a sweep that pins only the headline knob ships
# the rest unguarded.
# --------------------------------------------------------------------------------


def _chunk_s() -> float:
    """Resolved parakeet chunk seconds, read the way production reads it: at
    `ChunkedTranscriber` construction with no explicit override."""
    return ChunkedTranscriber(model_name="parakeet-tdt-0.6b-v2").chunk_duration_s


def _overlap_s() -> float:
    """Resolved parakeet overlap seconds. NOTE: construction also applies the
    joint chunk/overlap clamp — see `test_file_set_incompatible_pair_is_clamped`."""
    return ChunkedTranscriber(model_name="parakeet-tdt-0.6b-v2").overlap_duration_s


# key, env var, filename under CONFIG_DIR, resolver, default, a valid in-bounds
# sample distinct from the default, and an out-of-bounds sample.
KNOBS = [
    (
        "parakeet-chunk-s",
        "TAPSCRIBE_PARAKEET_CHUNK_S",
        "parakeet-chunk-s.txt",
        _chunk_s,
        120.0,
        300.0,
        100_000,
    ),
    (
        "parakeet-overlap-s",
        "TAPSCRIBE_PARAKEET_OVERLAP_S",
        "parakeet-overlap-s.txt",
        _overlap_s,
        15.0,
        20.0,
        5_000,
    ),
    (
        "summarize-timeout-s",
        "TAPSCRIBE_SUMMARIZE_TIMEOUT_S",
        "summarize-timeout-s.txt",
        _default_timeout_s,
        120.0,
        600.0,
        100_000,
    ),
    (
        "summarize-gguf-ctx",
        "TAPSCRIBE_SUMMARIZE_GGUF_CTX",
        "summarize-gguf-ctx.txt",
        default_gguf_ctx,
        8192,
        16384,
        1_000_000,
    ),
    (
        "diarize-threshold",
        "TAPSCRIBE_DIARIZE_THRESHOLD",
        "diarize-threshold.txt",
        resolve_threshold,
        0.7,
        0.55,
        5.0,
    ),
    (
        "diarize-max-speakers",
        "TAPSCRIBE_DIARIZE_MAX_SPEAKERS",
        "diarize-max-speakers.txt",
        resolve_max_speakers,
        8,
        4,
        500,
    ),
]

_KNOB_IDS = [k[0] for k in KNOBS]
_ALL_ENVS = [k[1] for k in KNOBS]


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp CONFIG_DIR (every config-file constant repointed under it) with ALL
    four knob env vars cleared, so each test starts from a known baseline."""
    d = tmp_path / "config"
    d.mkdir()
    repoint_config_files(monkeypatch, d)
    for env in _ALL_ENVS:
        monkeypatch.delenv(env, raising=False)
    return d


def _write_knob_file(cfg: Path, filename: str, value: object) -> None:
    (cfg / filename).write_text(str(value), encoding="utf-8")


# --------------------------------------------------------------------------------
# Precedence ladder, per knob.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_config_file_used_when_env_unset(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # DISCRIMINATOR (RED at base): with no env var the dashboard-written config file
    # drives the knob. Base ignores the file entirely and returns the default.
    _write_knob_file(cfg, filename, sample)
    assert resolve() == sample, (
        f"{key}: with {env} unset, the value must come from the config file the dashboard writes"
    )


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_env_var_wins_over_config_file(
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    env: str,
    filename: str,
    resolve,
    default,
    sample,
    oob,
) -> None:
    # Guardrail: an explicit, VALID env var stays the override — the file must not
    # shadow it. (Green at base for the wrong reason — base reads only env — so it
    # is paired with the RED cases above/below rather than standing alone.)
    _write_knob_file(cfg, filename, sample)
    monkeypatch.setenv(env, str(default))
    assert resolve() == default, f"{key}: an explicit valid {env} must win over the config file"


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_default_when_neither_set(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # Guardrail: no env, no file → the module default, unchanged.
    assert resolve() == default, f"{key}: with neither source set the default must be unchanged"


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_config_file_is_read_at_use_time(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # DISCRIMINATOR (RED at base): a change to the file is reflected on the NEXT
    # resolve — no restart, no cached snapshot. This is what makes the knob
    # dashboard-tunable rather than merely file-configurable at boot.
    _write_knob_file(cfg, filename, sample)
    assert resolve() == sample
    _write_knob_file(cfg, filename, default)
    assert resolve() == default, f"{key}: a config-file change must apply at use-time, without a restart"


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_out_of_bounds_config_file_falls_back_to_default(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # Guardrail: the knob's EXISTING bounds apply to the file value too — an
    # out-of-range file value degrades to the default exactly like a bad env var,
    # so an unbounded value can never reach the consumer.
    _write_knob_file(cfg, filename, oob)
    assert resolve() == default, f"{key}: an out-of-bounds config-file value must fall back to the default"


# --------------------------------------------------------------------------------
# The load-bearing rung: a set-but-INVALID env var must not shadow a valid file.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
@pytest.mark.parametrize("bad_env", ["", "   ", "abc", "nan", "inf", "-99999999"])
def test_invalid_env_falls_through_to_config_file(
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_env: str,
    key: str,
    env: str,
    filename: str,
    resolve,
    default,
    sample,
    oob,
) -> None:
    # DISCRIMINATOR: env set-but-invalid + a VALID config file → the FILE value, not
    # the default. An env branch keyed on presence (`in os.environ`) returns the
    # default here and silently discards the operator's dashboard setting — the
    # empty-string case is the real systemd EnvironmentFile trigger.
    _write_knob_file(cfg, filename, sample)
    monkeypatch.setenv(env, bad_env)
    assert resolve() == sample, (
        f"{key}: a set-but-invalid {env}={bad_env!r} must fall through to the valid config file, "
        "not shadow it with the default"
    )


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
@pytest.mark.parametrize("bad_env", ["", "abc", "nan", "inf"])
def test_invalid_env_without_config_file_falls_back_to_finite_default(
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_env: str,
    key: str,
    env: str,
    filename: str,
    resolve,
    default,
    sample,
    oob,
) -> None:
    # Guardrail + the NaN pitfall: `float("nan")` parses without error and slips a
    # naive range check (`lo <= nan <= hi` is False for the wrong reason), so an
    # unguarded resolver can hand NaN to the consumer — where every comparison goes
    # quietly False. The resolved value must be the finite default.
    monkeypatch.setenv(env, bad_env)
    v = resolve()
    assert v == default
    assert math.isfinite(v), f"{key}: a non-finite env value must never reach the consumer"


# --------------------------------------------------------------------------------
# Taxonomy: the GGUF context window is an INT knob, the other three are FLOAT.
# A uniform "parse as float" fix passes every value assertion above while handing a
# float to a consumer that indexes/allocates with it.
# --------------------------------------------------------------------------------


def test_gguf_ctx_file_value_stays_an_int(cfg: Path) -> None:
    _write_knob_file(cfg, "summarize-gguf-ctx.txt", 16384)
    v = default_gguf_ctx()
    assert v == 16384
    assert isinstance(v, int) and not isinstance(v, bool), (
        "summarize-gguf-ctx is an int knob — a float resolved from the config file "
        "reaches a consumer that uses it as a context-window size"
    )


def test_gguf_ctx_rejects_a_non_integer_file_value(cfg: Path) -> None:
    # A fractional context window is not a legal value; it must degrade to the
    # default rather than truncate silently to 16384 or raise at use time.
    _write_knob_file(cfg, "summarize-gguf-ctx.txt", "16384.5")
    assert default_gguf_ctx() == 8192


@pytest.mark.parametrize(
    ("filename", "resolve"),
    [
        ("parakeet-chunk-s.txt", _chunk_s),
        ("summarize-timeout-s.txt", _default_timeout_s),
    ],
)
def test_float_knobs_accept_a_fractional_file_value(cfg: Path, filename: str, resolve) -> None:
    # The mirror of the int pin: the float knobs must NOT be narrowed to int by a
    # uniform "all knobs are ints" fix — 90.5 s is a legal chunk/timeout.
    _write_knob_file(cfg, filename, "90.5")
    assert resolve() == 90.5


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_whitespace_wrapped_file_value_resolves(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # A trailing newline is what a text editor and an atomic write both leave.
    (cfg / filename).write_text(f"  {sample}\n", encoding="utf-8")
    assert resolve() == sample, f"{key}: a whitespace-wrapped file value must still resolve"


# --------------------------------------------------------------------------------
# ADVERSARIAL — the joint chunk/overlap constraint, reached through the FILE.
#
# `env_float` validates each knob INDEPENDENTLY, so a legal-but-incompatible PAIR
# passes both bound checks and only blows up inside `chunk_windows` (per-WAV, at
# request time). `ChunkedTranscriber.__init__` resolves it once via `clamp_overlap`.
# Adding a second value SOURCE must not route around that clamp — this is the
# interaction a per-knob contract misses.
# --------------------------------------------------------------------------------


def test_file_set_incompatible_pair_is_clamped(cfg: Path) -> None:
    # chunk=10 from the file against the DEFAULT 15 s overlap: 15 > 10 * 0.9, so the
    # overlap must be clamped to 9.0. Unclamped, `chunk_windows` cannot terminate.
    _write_knob_file(cfg, "parakeet-chunk-s.txt", 10)
    t = ChunkedTranscriber(model_name="parakeet-tdt-0.6b-v2")
    assert t.chunk_duration_s == 10.0
    assert t.overlap_duration_s == pytest.approx(9.0), (
        "a config-file chunk value must go through the same joint clamp as the env value — "
        "an unclamped pair wedges chunk_windows on every WAV"
    )


def test_file_set_incompatible_pair_both_sides_is_clamped(cfg: Path) -> None:
    # Both halves from the file, still incompatible (20 > 12 * 0.9 = 10.8).
    _write_knob_file(cfg, "parakeet-chunk-s.txt", 12)
    _write_knob_file(cfg, "parakeet-overlap-s.txt", 20)
    t = ChunkedTranscriber(model_name="parakeet-tdt-0.6b-v2")
    assert t.chunk_duration_s == 12.0
    assert t.overlap_duration_s == pytest.approx(10.8)


def test_explicit_constructor_args_still_beat_both_sources(cfg: Path) -> None:
    # DO-NOT-TOUCH sibling, pinned positively: the existing explicit-override path
    # (callers passing chunk/overlap directly) must keep winning over BOTH the env
    # var and the new config file — adding a source must not reorder the top rung.
    _write_knob_file(cfg, "parakeet-chunk-s.txt", 300)
    t = ChunkedTranscriber(model_name="parakeet-tdt-0.6b-v2", chunk_duration_s=45.0, overlap_duration_s=5.0)
    assert t.chunk_duration_s == 45.0
    assert t.overlap_duration_s == 5.0


# --------------------------------------------------------------------------------
# The write-time validator, exercised DIRECTLY through `write_config` — the path the
# dashboard PUT /api/config/{key} takes, and otherwise untested.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_write_config_round_trips_and_the_resolver_reads_it(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # End-to-end: a dashboard write lands on disk under the pinned filename AND the
    # use-time resolver picks it up (env unset via the fixture).
    write_config(key, str(sample))
    assert read_config(key) == str(sample)
    assert (cfg / filename).exists(), f"{key}: must persist to {filename} under CONFIG_DIR"
    assert resolve() == sample


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
@pytest.mark.parametrize("bad", ["abc", "nan", "inf"])
def test_write_config_rejects_invalid(
    cfg: Path, bad: str, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    with pytest.raises(ValueError):
        write_config(key, bad)


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_write_config_rejects_out_of_bounds(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # The write-time check must reuse the knob's read-time bounds, so write
    # acceptance and use-time resolution can never diverge on the same input.
    with pytest.raises(ValueError):
        write_config(key, str(oob))


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_write_config_empty_clears_the_override(
    cfg: Path, key: str, env: str, filename: str, resolve, default, sample, oob
) -> None:
    # Empty clears the override and the knob returns to its default — the operator's
    # way to hand a knob back to the code default from the dashboard.
    write_config(key, str(sample))
    assert resolve() == sample
    write_config(key, "")
    assert read_config(key) == ""
    assert resolve() == default, f"{key}: clearing the override must restore the default"


# --------------------------------------------------------------------------------
# The OPERATOR LOG. Falling through a bad env var must stay VISIBLE: the knobs used
# to resolve through `env_float`/`env_int`, which print a one-line
# "[tapscribe] ignoring …" notice for a value they reject. A resolver that returns
# the right number silently passes every assertion above while deleting the only
# signal an operator has that their env var is a typo.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
@pytest.mark.parametrize("bad_env", ["abc", "-99999999"])
def test_invalid_env_is_reported_to_the_operator(
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_env: str,
    key: str,
    env: str,
    filename: str,
    resolve,
    default,
    sample,
    oob,
) -> None:
    # The notice is emitted once per distinct bad value (the resolvers run per
    # summarize/transcribe AND behind the ~2 Hz /api/state poll), so clear the
    # registry first — otherwise this pin would depend on test ORDER.
    monkeypatch.setattr(config, "_WARNED_ENV", {})
    _write_knob_file(cfg, filename, sample)
    monkeypatch.setenv(env, bad_env)

    assert resolve() == sample  # the value still falls through to the file
    out = capsys.readouterr().out
    assert env in out and "ignoring" in out, (
        f"{key}: a set-but-invalid {env}={bad_env!r} must still be reported — it is an "
        "operator typo, and the resolvers that replaced env_float inherited its notice"
    )


@pytest.mark.parametrize(
    ("key", "env", "filename", "resolve", "default", "sample", "oob"), KNOBS, ids=_KNOB_IDS
)
def test_a_valid_env_is_not_reported(
    cfg: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    key: str,
    env: str,
    filename: str,
    resolve,
    default,
    sample,
    oob,
) -> None:
    # The mirror: the notice must mark a MISTAKE, not narrate every resolve — a
    # per-poll line for a perfectly good env var would bury the one that matters.
    monkeypatch.setattr(config, "_WARNED_ENV", {})
    monkeypatch.setenv(env, str(sample))
    assert resolve() == sample
    assert "ignoring" not in capsys.readouterr().out


# --------------------------------------------------------------------------------
# The knob must reach a WARM adapter. `load_transcriber` caches a loaded model and
# only evicts it at idle-TTL 0, so an adapter constructed before the operator's
# save can outlive it — freezing chunk/overlap at construction makes the knob
# use-time in name only, while /api/state already reports the new value.
# --------------------------------------------------------------------------------


class _StubChunked(ChunkedTranscriber):
    """The base skeleton with the model call stubbed out (what both Parakeet
    adapters are, minus the weights)."""

    name = "stub"
    backend = "cpu"
    device = "CPU"

    def _transcribe_window(self, chunk_pcm, window):  # noqa: ARG002
        return ()


def test_a_cached_adapter_rereads_the_knobs_on_its_next_transcribe(cfg: Path, tmp_path: Path) -> None:
    t = _StubChunked(model_name="parakeet-tdt-0.6b-v2")
    assert t.chunk_duration_s == 120.0

    _write_knob_file(cfg, "parakeet-chunk-s.txt", 30)
    _write_knob_file(cfg, "parakeet-overlap-s.txt", 5)
    # Short and silent: the stub never looks at the PCM, only the window walk runs.
    # `wav_builders` owns the recorder format, so a RECORDER_SAMPLE_RATE change
    # can't leave this fixture behind.
    result = t.transcribe(seed_wav(tmp_path / "a.wav", amplitude=0, seconds=0.2))

    assert (t.chunk_duration_s, t.overlap_duration_s) == (30.0, 5.0), (
        "a dashboard save must reach the adapter the model cache is holding warm, "
        "not just the next freshly-constructed one"
    )
    assert result.quality_settings["chunk_duration_s"] == 30.0


def test_an_explicit_constructor_arg_survives_the_reread(cfg: Path, tmp_path: Path) -> None:
    # The re-read must not promote the operator knob over a caller's explicit
    # override — that top rung is pinned above and outranks BOTH value sources.
    t = _StubChunked(model_name="parakeet-tdt-0.6b-v2", chunk_duration_s=45.0, overlap_duration_s=5.0)
    _write_knob_file(cfg, "parakeet-chunk-s.txt", 30)
    t.transcribe(seed_wav(tmp_path / "b.wav", amplitude=0, seconds=0.2))
    assert (t.chunk_duration_s, t.overlap_duration_s) == (45.0, 5.0)


def test_idle_ttl_key_is_untouched(cfg: Path) -> None:
    # DO-NOT-TOUCH sibling, pinned positively: #347's landed knob is the template
    # this slice copies, and a sweep over CONFIG_KEYS must not rewrite it.
    write_config("model-idle-ttl", "600")
    assert read_config("model-idle-ttl") == "600"
    with pytest.raises(ValueError):
        write_config("model-idle-ttl", "abc")


# --------------------------------------------------------------------------------
# The specialist table — surfaced READ-ONLY, and it must STAY launch-time-read.
# --------------------------------------------------------------------------------


def test_specialist_table_has_no_write_path(cfg: Path) -> None:
    # Anti-scope-creep pin: #210 asks for VISIBILITY of the specialist map, not
    # editability ("editable later"). A `specialist` config key would make it
    # use-time-read and silently break every test that monkeypatches
    # SPECIALIST_MODELS by setitem.
    from tapscribe.config_store import CONFIG_KEYS

    assert not [k for k in CONFIG_KEYS if "specialist" in k], (
        "the specialist map is surfaced read-only in #210 — adding a writable config key "
        "would turn a launch-time knob into a use-time one, out of scope"
    )


def test_specialist_table_is_still_read_at_import(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Guardrail: setting the env var now must NOT change the already-imported table.
    # The existing suite depends on `monkeypatch.setitem(SPECIALIST_MODELS, ...)`.
    from tapscribe.transcribers.catalog import SPECIALIST_MODELS

    before = dict(SPECIALIST_MODELS)
    monkeypatch.setenv("TAPSCRIBE_SPECIALIST_NO", "some-other-model")
    from tapscribe.transcribers.catalog import SPECIALIST_MODELS as after

    assert dict(after) == before, "the specialist table must remain a launch-time knob"
