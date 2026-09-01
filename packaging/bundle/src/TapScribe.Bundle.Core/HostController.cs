namespace TapScribe.Bundle.Core;

/// <summary>
/// The local Recorder as the tray's menu shows it: one line of state, and which of Start /
/// Stop Recorder the operator may reach.
/// </summary>
/// <param name="Header">One line of Recorder state. It goes in the MENU rather than on the
/// tray icon, which stays the Bridge's tap state (ADR-0022): the icon is what an operator
/// watches during a call, and a Recorder that is merely stopped must not read as a meeting
/// that failed.</param>
/// <param name="Alert">Whether this state is one the operator must be told about out loud
/// rather than on next opening the menu. A Recorder that crashed or failed to boot leaves NO
/// ambient signal otherwise: the tray icon stays the Bridge's tap state by design, so the
/// only cue would be a greyed line the operator has to right-click to find. False for a stop
/// they asked for — a balloon out of a click whose outcome they already know is noise.</param>
/// <remarks>There is deliberately no "is it ours" flag: a shell that wanted one would be
/// re-deriving what <paramref name="CanStop"/> already says, since an unmanaged Recorder is
/// exactly the one this tray may not stop, and the header says so in words.</remarks>
public sealed record HostView(string Header, bool CanStart, bool CanStop, bool Alert = false);

/// <summary>
/// Everything the host role needs from a tray shell. Its own seam in
/// <b>Bundle</b>.Core rather than a member on the Bridge's <c>ITrayView</c>, because a
/// Bundle is not a Bridge (CONTEXT.md): shipping one inside the other is composition, and
/// the composition happens in the SHELL, which is the one place both roles meet. Putting
/// this on <c>ITrayView</c> would make the Bridge's core carry a view model for a Recorder
/// it knows nothing about, or force a reference from the Bridge to the Bundle.
/// </summary>
public interface IHostView
{
    /// <summary>
    /// Render the local Recorder's section, or take it down. <c>null</c> means this install
    /// carries no host payload, and the section is then ABSENT rather than merely disabled:
    /// a bridge-only tray's menu is exactly what it was before the role existed. Called on
    /// the shell's UI thread.
    /// </summary>
    void ShowHost(HostView? host);
}

/// <summary>
/// The Recorder lifecycle a <see cref="HostController"/> drives — what
/// <see cref="RecorderSupervisor"/> offers, and nothing more. A seam so the menu's rules
/// are tested without spawning anything, and so "Quit stops only what this tray started"
/// is a property a test can hold the controller to.
/// </summary>
public interface IRecorderHost : IDisposable
{
    /// <summary>Whether the Recorder on the port is one this tray started.</summary>
    bool Manages { get; }

    /// <summary>Boot it. The returned task is the boot, not the Recorder's lifetime.</summary>
    Task Start();

    /// <summary>Stop the Recorder this tray started, if any.</summary>
    void Stop();
}

/// <summary>
/// The host role: boot, supervise and reap a co-located Recorder, and be the way in to it.
///
/// One per tray, and only when a host payload sits beside the tray on disk
/// (<see cref="BundleLayout.HostPayloadPresent"/>) — the role is a fact about the install,
/// not a flag, a build variant or a setting anyone can misconfigure (ADR-0022). The shell
/// asks; this class is what the answer gets it.
///
/// It is the <see cref="RecorderSupervisor"/>'s presentation half, the way
/// <c>BridgeRuntime</c> is the capture lifecycle's: the supervisor decides what the
/// Recorder is doing, and this decides what the menu says about it and which commands are
/// live. Marshalling is the shell's, through the <paramref name="post"/> it supplies —
/// state arrives on the supervisor's background thread and <see cref="IHostView"/> promises
/// the UI one.
/// </summary>
public sealed class HostController : IDisposable
{
    private readonly IHostView _view;
    private readonly Action<Action> _post;
    private readonly IRecorderHost _supervisor;
    private readonly Lock _gate = new();
    private RecorderState _state = RecorderState.Stopped;
    private string _message = "TapScribe is not running.";

    public HostController(IHostView view, Action<Action> post, IRecorderHost supervisor)
    {
        ArgumentNullException.ThrowIfNull(view);
        ArgumentNullException.ThrowIfNull(post);
        ArgumentNullException.ThrowIfNull(supervisor);
        _view = view;
        _post = post;
        _supervisor = supervisor;
    }

    /// <summary>
    /// Build the role over a Bundle's own layout: the supervisor, its reaper and its
    /// log writer, wired to this controller's state callback. The shell calls this once,
    /// after it has established that the payload is there.
    /// </summary>
    public static HostController Attach(
        IHostView view,
        Action<Action> post,
        BundleLayout layout,
        IProcessReaper? reaper,
        Action<string> log,
        Func<bool> recorderAnswers)
    {
        ArgumentNullException.ThrowIfNull(view);
        HostController? controller = null;
        var supervisor = new RecorderSupervisor(
            layout,
            reaper,
            log,
            onState: (state, message) => controller?.Report(state, message),
            recorderAnswers: recorderAnswers);
        controller = new HostController(view, post, supervisor);
        return controller;
    }

    /// <summary>Boot the Recorder and render the section for the first time.</summary>
    public void Start()
    {
        Report(RecorderState.Preflight, "Starting TapScribe…");
        _supervisor.Start();
    }

    /// <summary>
    /// What the supervisor reports, rendered. Public because it IS the seam between the
    /// two halves — <see cref="Attach"/> wires the supervisor's state callback straight to
    /// it — and because a test that had to reach a private method to drive the menu would
    /// be testing the reflection.
    /// </summary>
    public void Report(RecorderState state, string message) => Report(state, message, alert: true);

    private void Report(RecorderState state, string message, bool alert)
    {
        lock (_gate)
        {
            _state = state;
            _message = message;
        }
        HostView view = Render(state, message) with { Alert = alert && IsBad(state) };
        _post(() => _view.ShowHost(view));
    }

    /// <summary>States the operator has to hear about: the Recorder is not there and they
    /// did not ask for that. <c>Stopped</c> counts — it is what a crash-loop reports.</summary>
    private static bool IsBad(RecorderState state) =>
        state is RecorderState.Failed or RecorderState.Stopped;

    /// <summary>
    /// The operator asked for the Recorder to start (again). Refused when the menu would
    /// not have offered it — asked of <see cref="Render"/> rather than re-derived, so a new
    /// <see cref="RecorderState"/> cannot be enabled in one place and refused in the other.
    /// The claim happens INSIDE the lock, so a double-click cannot start two preflights.
    /// </summary>
    public void StartRecorder()
    {
        lock (_gate)
        {
            if (!Render(_state, _message).CanStart)
                return;
            _state = RecorderState.Preflight;
        }
        Start();
    }

    /// <summary>
    /// The operator asked for the Recorder to stop. Stops ONLY a Recorder this tray
    /// started: an unmanaged one — a <c>start.sh</c> in a terminal, another install — is
    /// not ours to kill, and the menu item is disabled for it anyway.
    /// </summary>
    public void StopRecorder()
    {
        lock (_gate)
        {
            // Asked of Render for the same reason StartRecorder is, rather than of the
            // supervisor: a new RecorderState must not be reachable here while the menu
            // that offers it says otherwise.
            if (!Render(_state, _message).CanStop)
                return;
        }
        if (!_supervisor.Manages)
            return;
        _supervisor.Stop();
        // No alert: the operator asked for this one.
        Report(RecorderState.Stopped, "TapScribe is not running.", alert: false);
    }

    private static HostView Render(RecorderState state, string message) => state switch
    {
        // Nothing to offer while it is coming up: a second Start would spawn a second
        // preflight, and Stop has nothing to stop yet.
        RecorderState.Preflight => new HostView(message, CanStart: false, CanStop: false),
        RecorderState.Running => new HostView(message, CanStart: false, CanStop: true),
        // Shown, and deliberately UNSTOPPABLE. Start is offered because the operator may
        // stop the other one themselves and want this tray to take over.
        RecorderState.Unmanaged => new HostView(message, CanStart: true, CanStop: false),
        // Stopped or Failed: the way out is to try again.
        _ => new HostView(message, CanStart: true, CanStop: false),
    };

    public void Dispose() => _supervisor.Dispose();
}
