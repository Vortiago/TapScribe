"""Reconcile the Recorder's live channel toward a desired state.

`/api/live/start` and the boot-time auto-start both need the same
transition: validate a requested (model, language, gate config), swap the
concrete `LiveChannel` family if the model needs a different engine, and
(re)start it — while a rejected request leaves a running channel exactly
as it was (#334). This module owns that transition as two FastAPI-free
functions so the route and the lifespan share ONE implementation instead
of each re-deriving the sequence:

  * `plan_live(current, desired, *, use_mlx)` — PURE. Resolves the family
    swap, validates the request against the catalog and the *target*
    channel's VAD capability, and decides no-op / gate-knob-only /
    restart. Raises a `LiveReconcileError` (mapped to an HTTP status by
    the app's domain-error table) BEFORE anything is touched, so a
    validation failure cannot disturb a running channel.

  * `apply_live(current, plan, *, set_live)` — the side effects: stop the
    old engine on a swap, install the target via `set_live`, announce the
    transition, and (re)start. Returns the result dict the route echoes.

The route/lifespan own the live slot; `set_live` installs the target at
the right moment in the choreography (so `/api/state` shows the new
engine "starting" during a multi-second reload). Nothing imports this
module back — it depends only on `live` / `moonshine_live` /
`transcribers.catalog`, never the Recorder, so there is no cycle. Kept
FastAPI-free (pinned by `test_domain_errors_fastapi_free`): validation
raises the domain errors below, never `HTTPException`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .live import GATE_KINDS, LiveChannel, gate_kind_error
from .moonshine_live import resolve_live_channel_for_model
from .transcribers.catalog import REGISTRY


class LiveReconcileError(Exception):
    """Base for live-channel reconcile failures. The concrete subclasses
    are registered in `routes.errors.DOMAIN_ERROR_STATUS`, so a route that calls
    `plan_live` stays a thin shim — the shared handler maps each to its
    status, exactly like the batch orchestrators' domain errors."""


class LiveModelUnknown(LiveReconcileError):
    """A CHANGED live model that is not an available live-context entry in
    the catalog (→ 400). Reusing the current pinned model is exempt."""


class GateKindUnsupported(LiveReconcileError):
    """gate_kind outside the allowlist, or `"backend"` requested against a
    channel with no native VAD (→ 400)."""


class LiveStartFailed(LiveReconcileError):
    """The engine (re)start failed; carries the engine's message (→ 500)."""


@dataclass(frozen=True)
class DesiredLiveState:
    """A parse-once request to reconcile the live channel toward. Every
    field is optional; `None` means "reuse the channel's current value"
    (a blank model/language means the same — "no override"). Built by the
    route from the request body and by the lifespan from persisted config;
    it is the test surface for `plan_live`."""

    model: str | None = None
    language: str | None = None
    gate_kind: str | None = None
    conf: bool | None = None
    gate_speech_threshold: float | None = None
    gate_hangover_ms: int | None = None
    gate_pre_roll_ms: int | None = None
    gate_min_speech_ms: int | None = None


@dataclass(frozen=True)
class LivePlan:
    """The computed transition `apply_live` executes.

    `target` is the channel to run — either `current` (no family swap) or
    a freshly constructed sibling (swap). `swap` records whether `target`
    differs from `current` (so `apply_live` stops the old one and installs
    the new). `no_restart` is the fast path: the running engine already
    satisfies the request, so only gate knobs are applied.
    """

    desired: DesiredLiveState
    target: LiveChannel
    swap: bool
    no_restart: bool


def plan_live(current: LiveChannel, desired: DesiredLiveState, *, use_mlx: bool) -> LivePlan:
    """Validate `desired` against `current` and decide the transition. Pure
    — constructs the target channel (side-effect-free) but touches no
    running engine, so raising here leaves a live channel untouched (#334).
    """
    # Resolve the family swap UNCONDITIONALLY. A persisted Moonshine
    # default at boot leaves `config.model` unchanged as a string, yet the
    # always-WhisperLiveKit boot channel still needs a swap to a Moonshine
    # engine (#259) — so the swap can NOT be gated on "model changed".
    # Only the catalog CHECK below is.
    target_model = desired.model or current.config.model
    new_channel = resolve_live_channel_for_model(current, target_model=target_model, use_mlx=use_mlx)
    target: LiveChannel = new_channel if new_channel is not None else current
    swap = new_channel is not None

    # The catalog is the allowlist: a CHANGED model must resolve to an
    # available live-context entry before it can reach an engine loader or
    # an HF Hub download. Re-sending the current model verbatim is exempt —
    # operators can pin an uncataloged WhisperLiveKit name via --live-model,
    # and the dashboard echoes the running selection on every Apply, so a
    # gate-knob/language tweak on a pinned model must not 400. Mirrors the
    # summarizer allowlist's "operator-controlled, not external input"
    # carve-out (PRD #120 story 23).
    if desired.model is not None and desired.model != current.config.model:
        entry = REGISTRY.get(desired.model)
        if entry is None or not entry.available or not entry.supports_context("live"):
            raise LiveModelUnknown(
                f"unknown live model {desired.model!r} — not a live-context entry in the "
                f"model catalog (see GET /api/models?context=live)"
            )

    # gate_kind is judged against the TARGET channel's capabilities (the
    # post-swap one): "backend" on a no-native-VAD engine would leave no
    # gate at all.
    if desired.gate_kind is not None and desired.gate_kind not in GATE_KINDS:
        raise GateKindUnsupported(gate_kind_error(desired.gate_kind))
    if desired.gate_kind == "backend" and not target.supports_native_vad:
        raise GateKindUnsupported(
            "requested live channel has no native VAD; gate_kind='backend' is not supported"
        )

    # A family swap always restarts (the fresh channel isn't running).
    # Otherwise the running engine may already satisfy the request, in
    # which case only gate knobs need applying (no restart).
    no_restart = not swap and target.matches(
        model=desired.model,
        language=desired.language,
        gate_kind=desired.gate_kind,
        conf=desired.conf,
    )
    return LivePlan(desired=desired, target=target, swap=swap, no_restart=no_restart)


def apply_live(
    current: LiveChannel,
    plan: LivePlan,
    *,
    set_live: Callable[[LiveChannel], None],
) -> dict[str, object]:
    """Execute `plan`. Blocking (spawns/kills an engine, may fetch weights)
    — call under `asyncio.to_thread`. `set_live` installs the target into
    the caller's slot at the right moment in the choreography. Raises
    `LiveStartFailed` if the (re)start fails."""
    desired = plan.desired
    target = plan.target

    if plan.no_restart:
        # Running engine already satisfies the request — apply any gate-knob
        # change to config (the next /tap's SpeechGate reads it) without a
        # restart, and report it distinctly.
        current.apply_gate_knobs(
            gate_speech_threshold=desired.gate_speech_threshold,
            gate_hangover_ms=desired.gate_hangover_ms,
            gate_pre_roll_ms=desired.gate_pre_roll_ms,
            gate_min_speech_ms=desired.gate_min_speech_ms,
        )
        return {
            "ok": True,
            "msg": "already running; any gate-knob change applied without restart",
            "state": current.info["state"],
        }

    if plan.swap:
        # Free the old engine's port/child before installing the sibling,
        # then install so `/api/state` reflects the new engine immediately.
        if current.running():
            current.stop()
        set_live(target)

    # Announce the transition (writes gate config + conf, flips info to
    # "starting" with the new model/language) BEFORE teardown/weights, so a
    # dashboard polling mid-swap renders the new selection, not the old.
    target.begin_transition(
        model=desired.model,
        language=desired.language,
        gate_kind=desired.gate_kind,
        conf=desired.conf,
        gate_speech_threshold=desired.gate_speech_threshold,
        gate_hangover_ms=desired.gate_hangover_ms,
        gate_pre_roll_ms=desired.gate_pre_roll_ms,
        gate_min_speech_ms=desired.gate_min_speech_ms,
    )

    if target.running():
        target.stop()
        # stop() sets state="stopped"; re-announce so the dashboard stays on
        # "starting" with the new model through the multi-second reload.
        target.begin_transition(model=desired.model, language=desired.language)

    ok, msg = target.start(model=desired.model, language=desired.language)
    if not ok:
        raise LiveStartFailed(msg)
    return {"ok": True, "msg": msg, "state": target.info["state"]}
