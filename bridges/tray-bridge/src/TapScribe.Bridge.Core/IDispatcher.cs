namespace TapScribe.Bridge.Core;

/// <summary>
/// Runs a callback on the shell's UI thread. The meeting lifecycle does its work on
/// thread-pool continuations and marshals every view touch back through here.
///
/// This is a seam rather than a <see cref="SynchronizationContext"/> because AppKit does not
/// have one: .NET installs no SynchronizationContext on macOS, and the main run loop is
/// reached through <c>DispatchQueue.MainQueue</c>. The WinForms shell wraps its
/// WindowsFormsSynchronizationContext in one of these; the AppKit shell wraps the main queue.
/// Taking it as a constructor dependency also removes the "Current is null means the caller
/// is on the wrong thread" check the shell used to repeat at four entry points: an invariant
/// that is simply false on macOS, where Current is null everywhere.
/// </summary>
public interface IDispatcher
{
    /// <summary>Queue <paramref name="action"/> for the UI thread and return immediately.
    /// Never blocks: a runtime that waited on the UI thread from a continuation the UI thread
    /// is itself waiting on would deadlock, so there is deliberately no Send counterpart.</summary>
    void Post(Action action);
}
