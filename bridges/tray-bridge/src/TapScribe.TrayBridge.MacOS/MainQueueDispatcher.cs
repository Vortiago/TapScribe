using CoreFoundation;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The Mac shell's <see cref="IDispatcher"/>: the main dispatch queue, the thread AppKit may be
/// touched from. There is no <c>SynchronizationContext</c> to wrap here, which is the whole reason
/// the seam is <see cref="IDispatcher"/> rather than a context.
///
/// It queues, always, and never asks whether the caller is already on the main thread. That
/// shortcut is not free: the runtime posts a status and then the menu state that goes with it, and
/// running one inline while the other queues puts them on screen in the wrong order. A posted
/// callback that throws is left to escape, matching the WinForms sibling.
/// </summary>
internal sealed class MainQueueDispatcher : IDispatcher
{
    // Resolved once. DispatchQueue.MainQueue is a PROPERTY that builds a fresh managed wrapper per
    // access (32 bytes and a finalizable object each), and Post runs on every marshalled callback.
    // The main queue is a process singleton, so caching it changes nothing else.
    private static readonly DispatchQueue Main = DispatchQueue.MainQueue;

    private readonly Action<Action> _enqueue;

    /// <summary>The real thing: work goes to <c>DispatchQueue.MainQueue</c>.</summary>
    internal MainQueueDispatcher()
        : this(static work => Main.DispatchAsync(work))
    {
    }

    /// <summary>Build over an explicit queue, the seam the dispatcher's own tests drive: the
    /// main queue delivers nothing until AppKit's run loop is up, so the queueing contract
    /// would otherwise be observable only in a running app.</summary>
    /// <param name="enqueue">Hands one unit of work to the queue and returns.</param>
    internal MainQueueDispatcher(Action<Action> enqueue)
    {
        ArgumentNullException.ThrowIfNull(enqueue);
        _enqueue = enqueue;
    }

    /// <summary>Queue <paramref name="action"/> for the main thread and return.</summary>
    public void Post(Action action)
    {
        ArgumentNullException.ThrowIfNull(action);
        _enqueue(action);
    }
}
