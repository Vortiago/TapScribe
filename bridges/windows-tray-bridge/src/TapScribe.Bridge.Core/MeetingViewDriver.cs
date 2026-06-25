namespace TapScribe.Bridge.Core;

/// <summary>
/// The surface a per-meeting window presents to <see cref="MeetingViewDriver"/>: render the
/// latest poll view (or <c>null</c> for the pre-first-poll loading state), and report whether
/// it has been disposed (closed) so the driver can stop touching it. The WinForms
/// <c>MeetingForm</c> implements this; tests use a fake — so the open-flow presenter is
/// exercised cross-platform, with no WinForms dependency.
/// </summary>
public interface IMeetingView
{
    void Render(PipelineView? view);
    bool IsDisposed { get; }
}

/// <summary>
/// Drives an <see cref="IMeetingView"/> from a <see cref="MeetingController"/>'s emissions,
/// marshalled to the UI thread via the supplied <see cref="SynchronizationContext"/>, and
/// surfaces a clear "couldn't load" state when the Recorder is unreachable. This is the
/// testable heart of opening a past meeting (#168): it owns NO network / settings / window
/// lifecycle (the caller builds the <see cref="ControlClient"/> + controller and owns the
/// <see cref="CancellationTokenSource"/>), so it is exercised end to end against a fake
/// Recorder with a fake view — no WinForms, hence cross-platform-testable like the rest of
/// the core. The WinForms shell only implements <see cref="IMeetingView.Render"/>.
/// </summary>
public static class MeetingViewDriver
{
    /// <summary>Subscribe the view to the controller's poll emissions (marshalled to
    /// <paramref name="ui"/>), then ride <see cref="MeetingController.ResumeAsync"/> to the
    /// terminal summary / failure. A transient-or-unreachable error renders a clear failure in
    /// the view instead of leaving it on "Loading…".</summary>
    public static async Task DriveAsync(MeetingController controller, IMeetingView view,
        SynchronizationContext ui, CancellationToken cancellationToken)
    {
        // Marshal a render to the UI thread, guarding IsDisposed: a poll emission posted just
        // before the window closed must not touch disposed controls.
        void RenderSafe(PipelineView? poll) => ui.Post(_ => { if (!view.IsDisposed) view.Render(poll); }, null);
        controller.Updated += poll => RenderSafe(poll);

        try
        {
            await controller.ResumeAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (Exception ex) when (
            ex is HttpRequestException or OperationCanceledException or InvalidOperationException)
        {
            // The Recorder is unreachable / timed out (or the window was closed mid-poll —
            // OperationCanceledException, benign). Surface a clear failure in the view rather
            // than leaving it on "Loading…". The filter keeps this off CodeQL's catch-all radar.
            if (!cancellationToken.IsCancellationRequested)
                RenderSafe(PipelineView.Unavailable("Couldn't reach the recorder to load this meeting."));
        }
    }
}
