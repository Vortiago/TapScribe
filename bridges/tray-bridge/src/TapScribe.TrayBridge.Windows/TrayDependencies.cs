using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge.Windows;

/// <summary>
/// Everything outside the tray SHELL that it needs: the meeting runtime's own outside world
/// (<see cref="BridgeDependencies"/>) plus the two things only a WinForms tray has.
/// <see cref="Production"/> is what the app runs, so the seam is a single substitution at
/// construction and the shell's own code is identical under test and in production.
/// </summary>
/// <param name="Bridge">What a meeting needs: the enumerator, the session mint and the three
/// stores. Passed straight to <see cref="BridgeRuntime"/>, which is where the lifecycle lives:
/// the shell neither reads nor writes any of it.</param>
/// <param name="CreateIndicator">The tray's notification-area presence, see
/// <see cref="ITrayIndicator"/> for why the OS-facing sliver is separable at all.</param>
/// <param name="ScheduleOnLoopStart">Runs a callback once, on the UI thread, as soon as the
/// message loop is pumping. The shell builds its runtime there, because that is the first
/// moment SynchronizationContext.Current is the WinForms one. Production schedules it with a
/// one-shot WinForms timer, which registers a native timer window on the calling thread, so a
/// caller that will never pump a loop supplies its own scheduling rather than leaving that
/// window behind on a thread that then goes away.</param>
internal sealed record TrayDependencies(
    BridgeDependencies Bridge,
    Func<ITrayIndicator> CreateIndicator,
    Action<Action> ScheduleOnLoopStart)
{
    public static TrayDependencies Production { get; } = new(
        new BridgeDependencies(
            static () => new WasapiDeviceEnumerator(),
            MintDetachedSessionAsync,
            TrayStores.Settings,
            TrayStores.MeetingState,
            TrayStores.MeetingHistory),
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
