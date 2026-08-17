namespace TapScribe.Bridge.Core;

/// <summary>
/// Everything outside the shell that a meeting needs: the audio device enumerator, the
/// detached-session mint, and the three persistent stores. One record rather than six
/// constructor parameters, and each platform supplies its own instance: so the seam is a
/// single substitution at construction and <see cref="BridgeRuntime"/>'s own code is
/// identical under test, on Windows and on macOS.
///
/// The stores are the concrete Core types, not an interface over them: after slice 0A each
/// takes its directory by injection, so a test points them at a temp directory and exercises
/// the real serialization instead of a fake's approximation of it. That is what retired the
/// shell's own <c>IMeetingStores</c>: it existed only to keep tests off the operator's real
/// %APPDATA%, which the directory parameter now does better.
/// </summary>
/// <param name="OpenEnumerator">Opens a device enumerator for one meeting. Ownership passes
/// to the caller, which releases it when the meeting ends.</param>
/// <param name="MintDetachedSession">Mints the detached session a meeting taps into, doubling
/// as the connection pre-flight: it throws when the Recorder is unreachable or refuses the
/// token, before any device is opened.</param>
/// <param name="SettingsStore">Persists the operator's settings on Save.</param>
/// <param name="StateStore">The restart-resume state for an in-flight pipeline.</param>
/// <param name="HistoryStore">The Past-meetings history (#168).</param>
public sealed record BridgeDependencies(
    Func<IAudioDeviceEnumerator> OpenEnumerator,
    Func<BridgeSettings, CancellationToken, Task<string>> MintDetachedSession,
    BridgeSettingsStore SettingsStore,
    MeetingStateStore StateStore,
    MeetingHistoryStore HistoryStore);
