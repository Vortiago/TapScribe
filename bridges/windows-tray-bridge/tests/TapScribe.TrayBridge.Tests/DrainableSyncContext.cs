using System.Collections.Concurrent;
using System.Diagnostics;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// A deterministic <see cref="SynchronizationContext"/> for the WinForms E2E: <see cref="Post"/>
/// queues the callback and the test drains it onto the STA thread by hand — no message loop and
/// no <c>WindowsFormsSynchronizationContext</c> timing to fight. This models exactly what the
/// tray's UI thread does with <see cref="MeetingWindowDriver"/>'s render posts: the controller
/// raises Updated on a thread-pool poll continuation, which Posts a render here; the UI thread
/// (here, the test's pump) dispatches it onto the real <see cref="MeetingForm"/>.
/// </summary>
internal sealed class DrainableSyncContext : SynchronizationContext
{
    private readonly BlockingCollection<(SendOrPostCallback Callback, object? State)> _queue = new();

    public override void Post(SendOrPostCallback d, object? state) => _queue.Add((d, state));

    /// <summary>Dispatch queued callbacks until <paramref name="done"/> is true or
    /// <paramref name="timeout"/> elapses, then flush anything still queued (the final render
    /// posted in the same instant the driver finished).</summary>
    public void PumpUntil(Func<bool> done, TimeSpan timeout)
    {
        var elapsed = Stopwatch.StartNew();
        while (!done() && elapsed.Elapsed < timeout)
            if (_queue.TryTake(out (SendOrPostCallback Callback, object? State) item, millisecondsTimeout: 20))
                item.Callback(item.State);
        while (_queue.TryTake(out (SendOrPostCallback Callback, object? State) item, millisecondsTimeout: 0))
            item.Callback(item.State);
    }
}
