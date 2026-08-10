using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray's two %APPDATA% stores behind one seam. The shell reads and writes them from
/// the meeting lifecycle, so a test that drives End or the Past-meetings menu would
/// otherwise touch the operator's REAL resume state and meeting history — the same reason
/// every store already carries a path overload.
/// </summary>
internal interface IMeetingStores
{
    MeetingState? LoadState();
    void SaveState(MeetingState state);
    void ClearState();
    MeetingHistory LoadHistory();
    void AppendHistory(MeetingRecord record);
}

/// <summary>The production stores: the %APPDATA%\TapScribe files the operator's tray uses.</summary>
internal sealed class AppDataMeetingStores : IMeetingStores
{
    public MeetingState? LoadState() => MeetingStateStore.Load();

    public void SaveState(MeetingState state) => MeetingStateStore.Save(state);

    public void ClearState() => MeetingStateStore.Clear();

    public MeetingHistory LoadHistory() => MeetingHistoryStore.Load();

    public void AppendHistory(MeetingRecord record) => MeetingHistoryStore.Append(record);
}

/// <summary>
/// Everything outside the tray shell that a meeting needs: the audio device enumerator,
/// the detached-session mint, and the persistent stores. One record rather than four
/// constructor parameters, and <see cref="Production"/> is what the app runs — so the
/// seam is a single substitution at construction and the shell's own code is identical
/// under test and in production.
///
/// This is deliberately NOT the runtime extraction: the meeting lifecycle (resolve →
/// mint → open → publish → drain → teardown) stays in <see cref="TrayContext"/>, because
/// that lifecycle IS what the tray tests exercise. This only replaces its outside world.
/// </summary>
/// <param name="OpenEnumerator">Opens a device enumerator for one meeting (or one
/// Settings-dialog meter session). The caller owns it and disposes it when it is
/// <see cref="IDisposable"/>.</param>
/// <param name="MintDetachedSession">Mints the detached session a meeting taps into,
/// doubling as the connection pre-flight — it throws when the Recorder is unreachable or
/// refuses the token.</param>
/// <param name="Stores">The resume state and Past-meetings history.</param>
internal sealed record TrayDependencies(
    Func<IAudioDeviceEnumerator> OpenEnumerator,
    Func<BridgeSettings, CancellationToken, Task<string>> MintDetachedSession,
    IMeetingStores Stores)
{
    public static TrayDependencies Production { get; } = new(
        static () => new WasapiDeviceEnumerator(),
        MintDetachedSessionAsync,
        new AppDataMeetingStores());

    private static async Task<string> MintDetachedSessionAsync(
        BridgeSettings settings, CancellationToken cancellationToken)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        return await control.CreateDetachedSessionAsync(cancellationToken).ConfigureAwait(false);
    }
}
