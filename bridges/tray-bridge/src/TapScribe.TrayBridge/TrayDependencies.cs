using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The tray's two %APPDATA% stores behind one seam. The shell reads and writes them from
/// the meeting lifecycle, so a test that drives End or the Past-meetings menu would
/// otherwise touch the operator's REAL resume state and meeting history — the same reason
/// the stores themselves take their directory by injection.
/// </summary>
internal interface IMeetingStores
{
    MeetingState? LoadState();
    void SaveState(MeetingState state);
    void ClearState();
    MeetingHistory LoadHistory();
    void AppendHistory(MeetingRecord record);
}

/// <summary>The production stores: the <see cref="TrayStores"/> instances, which is where
/// %APPDATA%\TapScribe is spelled.</summary>
internal sealed class AppDataMeetingStores : IMeetingStores
{
    public MeetingState? LoadState() => TrayStores.MeetingState.Load();

    public void SaveState(MeetingState state) => TrayStores.MeetingState.Save(state);

    public void ClearState() => TrayStores.MeetingState.Clear();

    public MeetingHistory LoadHistory() => TrayStores.MeetingHistory.Load();

    public void AppendHistory(MeetingRecord record) => TrayStores.MeetingHistory.Append(record);
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
/// <param name="CreateIndicator">The tray's notification-area presence — see
/// <see cref="ITrayIndicator"/> for why the OS-facing sliver is separable at all.</param>
/// <param name="ScheduleOnLoopStart">Runs a callback once, on the UI thread, as soon as the
/// message loop is pumping. The tray's resume kick cannot run in the constructor (the
/// WinForms SynchronizationContext isn't installed until Application.Run), and production
/// schedules it with a one-shot WinForms timer — which registers a native timer window on
/// the calling thread. A caller that will never pump a loop supplies a no-op instead of
/// leaving that window behind on a thread that then goes away.</param>
internal sealed record TrayDependencies(
    Func<IAudioDeviceEnumerator> OpenEnumerator,
    Func<BridgeSettings, CancellationToken, Task<string>> MintDetachedSession,
    IMeetingStores Stores,
    Func<ITrayIndicator> CreateIndicator,
    Action<Action> ScheduleOnLoopStart)
{
    public static TrayDependencies Production { get; } = new(
        static () => new WasapiDeviceEnumerator(),
        MintDetachedSessionAsync,
        new AppDataMeetingStores(),
        static () => new NotifyIconIndicator(),
        ScheduleOnMessageLoop);

    // A one-shot UI-thread timer: the only way to get a callback onto the message loop from
    // a constructor that runs before Application.Run installs it. It disposes itself on the
    // single tick it exists for.
    private static void ScheduleOnMessageLoop(Action action)
    {
        var timer = new System.Windows.Forms.Timer { Interval = 200 };
        timer.Tick += (_, _) =>
        {
            timer.Stop();
            timer.Dispose();
            action();
        };
        timer.Enabled = true;
    }

    private static async Task<string> MintDetachedSessionAsync(
        BridgeSettings settings, CancellationToken cancellationToken)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        return await control.CreateDetachedSessionAsync(cancellationToken).ConfigureAwait(false);
    }
}
