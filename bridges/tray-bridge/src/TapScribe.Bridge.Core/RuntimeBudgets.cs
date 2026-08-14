namespace TapScribe.Bridge.Core;

/// <summary>
/// The runtime's time budgets, injected so no test has to spend them. Each is a BACKSTOP
/// rather than a promise: the values below are what the shell ships with, and a test that
/// needs a flow to settle quickly shortens them instead of sleeping.
/// </summary>
public sealed record RuntimeBudgets
{
    /// <summary>How often the end-of-meeting pipeline is polled for progress.</summary>
    public TimeSpan PollInterval { get; init; } = TimeSpan.FromSeconds(1.5);

    /// <summary>
    /// How long the session mint may take before Start gives up. Without a bound HttpClient
    /// waits its 100 s default, which would wedge the shell on "Starting…" against a host that
    /// accepts the connection and then never replies: exactly the case a test has to be able to
    /// reach without spending the real budget.
    /// </summary>
    public TimeSpan MintTimeout { get; init; } = TimeSpan.FromSeconds(20);

    /// <summary>
    /// How long teardown waits for a Start still in flight to reach the point where it can
    /// tear its own meeting down. A backstop, not a promise: the session mint it is usually
    /// blocked on carries its own 20 s timeout, and quitting must stay responsive against a
    /// Recorder that accepted the connection and then went quiet.
    /// </summary>
    public TimeSpan StartSettleTimeout { get; init; } = TimeSpan.FromSeconds(5);

    /// <summary>
    /// How long teardown waits for the pipelines to close. The orchestrator drains them
    /// concurrently and each is bounded, so this stays about one drain budget rather than N of
    /// them; a sub-second tail may drop on a hard quit.
    /// </summary>
    public TimeSpan QuitTeardownCap { get; init; } = TimeSpan.FromSeconds(5);
}
