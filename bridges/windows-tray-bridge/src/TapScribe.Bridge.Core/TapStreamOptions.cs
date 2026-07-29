namespace TapScribe.Bridge.Core;

/// <summary>
/// The Blip-resilience recipe for one <see cref="TapStream"/>: the reconnect
/// ladder, the during-gap buffer bound, and the drain budget. Recommended, not
/// enforced — the Recorder has no opinion on these (CONTEXT.md) — but the
/// bundled bridges must not drift from each other, so these defaults are pinned
/// against bridges/spacialchat-bridge/content.js and bridges/README.md by
/// tests/test_tap_wire_contract.py. Tests inject tight values so the resilience
/// paths run in milliseconds.
/// </summary>
public sealed record TapStreamOptions
{
    /// <summary>
    /// Reconnect delay by attempt index (0 = first retry). Attempts past the end
    /// of the list use <see cref="BackoffCap"/>. Jittered exponential by default.
    /// </summary>
    public IReadOnlyList<TimeSpan> Backoff { get; init; } =
    [
        TimeSpan.FromMilliseconds(200),
        TimeSpan.FromMilliseconds(400),
        TimeSpan.FromMilliseconds(800),
        TimeSpan.FromMilliseconds(1600),
        TimeSpan.FromMilliseconds(3200),
    ];

    /// <summary>Upper bound on any single reconnect delay.</summary>
    public TimeSpan BackoffCap { get; init; } = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Fractional jitter applied to each backoff (±this fraction), so a roomful of
    /// bridges reconnecting after the same outage don't synchronise. 0 disables.
    /// </summary>
    public double BackoffJitter { get; init; } = 0.25;

    /// <summary>
    /// Cap on PCM buffered while disconnected. Past this the oldest frames are
    /// dropped, so a long outage can't grow memory without bound — a multi-second
    /// outage loses its oldest tail, not the whole utterance. 96000 bytes ≈ 3 s of
    /// 16 kHz mono int16.
    /// </summary>
    public int MaxBufferBytes { get; init; } = 96_000;

    /// <summary>
    /// How long an ended utterance keeps trying to reach a /tap WS to flush its
    /// trailing buffered PCM before giving up. Bounds Drain so an unreachable
    /// Recorder can never wedge teardown forever.
    /// </summary>
    public TimeSpan DrainBudget { get; init; } = TimeSpan.FromSeconds(8);

    /// <summary>
    /// Backstop wake interval while connected with an empty buffer, so a drain /
    /// stop set without an accompanying enqueue is still noticed promptly. The
    /// fast path is the per-enqueue signal; this only bounds the idle case.
    /// </summary>
    public TimeSpan PollInterval { get; init; } = TimeSpan.FromMilliseconds(50);
}
