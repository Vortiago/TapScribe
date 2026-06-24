using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// Drives a <see cref="MeetingForm"/> from a <see cref="MeetingController"/>'s emissions,
/// marshalled to the WinForms UI thread, and surfaces a clear "couldn't load" state when the
/// Recorder is unreachable. This is the testable heart of opening a past meeting (#168): it
/// owns NO network / settings / window lifecycle (the caller builds the <see cref="ControlClient"/>
/// + controller and owns the <see cref="CancellationTokenSource"/>), so a Windows E2E test can
/// drive a real window from a real controller against a fake Recorder with no tray shell.
/// </summary>
internal static class MeetingWindowDriver
{
    /// <summary>Subscribe the window to the controller's poll emissions (marshalled to
    /// <paramref name="ui"/>), then ride <see cref="MeetingController.ResumeAsync"/> to the
    /// terminal summary / failure. A transient-or-unreachable error renders a clear failure
    /// in the window instead of leaving it on "Loading…".</summary>
    public static async Task DriveAsync(MeetingController controller, MeetingForm form,
        SynchronizationContext ui, CancellationToken cancellationToken)
    {
        // Guard IsDisposed: a poll emission posted just before the window closed must not
        // touch disposed controls.
        controller.Updated += view => ui.Post(_ => { if (!form.IsDisposed) form.Render(view); }, null);

        try
        {
            await controller.ResumeAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex) when (
            ex is HttpRequestException or OperationCanceledException or InvalidOperationException)
        {
            // The Recorder is unreachable / timed out (or the window was closed mid-poll —
            // OperationCanceledException, benign). Surface a clear failure in the window rather
            // than leaving it on "Loading…". The filter keeps this off CodeQL's catch-all radar.
            if (!cancellationToken.IsCancellationRequested)
                ui.Post(_ =>
                {
                    if (!form.IsDisposed)
                        form.Render(PipelineView.Unavailable("Couldn't reach the recorder to load this meeting."));
                }, null);
        }
    }
}
