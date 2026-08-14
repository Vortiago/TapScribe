namespace TapScribe.Bridge.Core;

/// <summary>
/// Which channel a notice belongs to. The tray has exactly two, and they differ in the
/// operator's reading of them rather than in their plumbing: a <see cref="Warning"/> is
/// something that went wrong and a <see cref="Information"/> is something that went right.
/// Named rather than a bool so a call site says which it means.
/// </summary>
public enum NoticeKind
{
    /// <summary>Something failed, or a device dropped out of a running meeting.</summary>
    Warning,

    /// <summary>A milestone worth surfacing: the summary is ready, the recording is saved.</summary>
    Information,
}

/// <summary>
/// Everything the meeting runtime needs from a tray shell, and nothing else. The WinForms
/// <c>TrayContext</c> implements it over a <c>NotifyIcon</c> and a <c>ContextMenuStrip</c>;
/// the AppKit shell implements it over an <c>NSStatusItem</c>: so the lifecycle in
/// <see cref="BridgeRuntime"/> is written once and tested without either.
///
/// Every method is called ON the shell's UI thread: the runtime marshals through its
/// <see cref="IDispatcher"/> before it touches the view, so an implementation never needs a
/// thread check of its own.
/// </summary>
public interface ITrayView
{
    /// <summary>Render the at-a-glance state: the menu header line, the icon and the tooltip.
    /// The runtime suppresses re-renders of the status already showing, so an implementation
    /// may apply this unconditionally.</summary>
    void ShowStatus(StatusView status);

    /// <summary>Surface a transient message (a Windows balloon, an AppKit notification).</summary>
    void ShowNotice(string title, string message, NoticeKind kind);

    /// <summary>Enable or disable the two meeting commands. Both false is a legitimate state:
    /// a meeting that is ending, or a pipeline in flight.</summary>
    void SetMenuState(bool canStart, bool canEnd);

    /// <summary>
    /// Open a window for one meeting's notes and hand it back for the runtime to render into.
    /// Each call is a NEW window: a finished meeting and a re-opened past one are independent,
    /// and neither may disturb the live status line or the Start/End commands.
    /// </summary>
    IMeetingWindow OpenMeetingWindow();

    /// <summary>
    /// Teardown has finished: release the shell's own UI and stop its event loop. Called once,
    /// at the END of <see cref="BridgeRuntime.QuitAsync"/>, so nothing is streaming and no
    /// callback is still in flight by the time it runs. Releasing the UI any earlier would
    /// leave the closing pipelines posting into a view that is already gone.
    /// </summary>
    void Shutdown();
}

/// <summary>
/// A per-meeting window: an <see cref="IMeetingView"/> the runtime renders poll emissions
/// into, plus the one thing the runtime needs back from it. <see cref="Closed"/> is what lets
/// the runtime stop polling the instant the operator closes the window, so a re-opened past
/// meeting does not keep talking to the Recorder for as long as the process lives.
/// </summary>
public interface IMeetingWindow : IMeetingView
{
    /// <summary>Raised on the UI thread when the operator closes the window.</summary>
    event Action? Closed;
}
