using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.ExceptionServices;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// A dedicated STA thread with a hand-pumped <see cref="SynchronizationContext"/> — the
/// harness every tray test drives <c>TrayContext</c> through.
///
/// Two reasons it exists. WinForms types are STA-affine and xunit v2 runs its tests on MTA
/// thread-pool threads, so a <c>NotifyIcon</c> / <c>ContextMenuStrip</c> built on the test
/// thread is built on the wrong kind of thread. And the shell marshals every callback
/// through <c>SynchronizationContext.Post</c>, which in production a WinForms message loop
/// pumps — a real loop here would mean <c>Application.Run</c> (which blocks, and would hang
/// CI) and non-deterministic timing. So <see cref="Post"/> QUEUES, and a test calls
/// <see cref="Drain"/> when it wants the shell's pending UI work to happen. That makes
/// "has the tray updated yet?" a decision the test makes rather than a race it runs.
///
/// No window is ever shown and no message loop is ever entered. Every wait is bounded, so a
/// wedged test fails rather than hanging the job.
/// </summary>
internal sealed class StaShell : IDisposable
{
    /// <summary>Bound on any single marshalled call. Generous — it is a backstop against a
    /// hang, not a timing assertion.</summary>
    private static readonly TimeSpan CallTimeout = TimeSpan.FromSeconds(30);

    private readonly BlockingCollection<Action> _work = new();
    private readonly QueueingContext _context = new();
    private readonly Thread _thread;

    public StaShell()
    {
        _thread = new Thread(Loop) { IsBackground = true, Name = "tray-shell-sta" };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        // The shell reads SynchronizationContext.Current when Start/End/OpenPastMeeting run,
        // exactly as it reads the WinForms one in production.
        Run(() => SynchronizationContext.SetSynchronizationContext(_context));
    }

    private void Loop()
    {
        foreach (Action work in _work.GetConsumingEnumerable())
            work();
    }

    /// <summary>Run <paramref name="action"/> on the STA thread and wait for it, rethrowing
    /// whatever it threw with its original stack — so a test reads like a direct call.</summary>
    public void Run(Action action)
    {
        ExceptionDispatchInfo? failure = null;
        using var done = new ManualResetEventSlim(false);
        _work.Add(() =>
        {
            try
            {
                action();
            }
            catch (Exception ex)
            {
                failure = ExceptionDispatchInfo.Capture(ex);
            }
            finally
            {
                done.Set();
            }
        });
        if (!done.Wait(CallTimeout))
            throw new TimeoutException("the STA shell thread did not finish the call in time");
        failure?.Throw();
    }

    public T Run<T>(Func<T> function)
    {
        T result = default!;
        Run(() => result = function());
        return result;
    }

    /// <summary>
    /// Run every callback the shell has posted so far, on the STA thread, in order — the
    /// hand-cranked equivalent of one turn of the WinForms message loop. Repeats while
    /// callbacks post further callbacks (<c>FailToIdle</c> does), bounded so a self-posting
    /// loop fails instead of spinning. Returns whatever the callbacks threw: a callback
    /// running AFTER Quit touches components Quit disposed, which in production simply never
    /// happens (the loop is gone by then), so it must not fail the test.
    /// </summary>
    public IReadOnlyList<Exception> Drain()
    {
        var errors = new List<Exception>();
        Run(() =>
        {
            for (int round = 0; round < 10; round++)
            {
                IReadOnlyList<SendOrPostCallback> batch = _context.Take(out object?[] states);
                if (batch.Count == 0)
                    return;
                for (int i = 0; i < batch.Count; i++)
                {
                    try
                    {
                        batch[i](states[i]);
                    }
                    catch (Exception ex)
                    {
                        errors.Add(ex);
                    }
                }
            }
            throw new InvalidOperationException("posted callbacks kept posting more; drained 10 rounds");
        });
        return errors;
    }

    /// <summary>How many callbacks the shell has posted and not yet had drained.</summary>
    public int Pending => _context.Pending;

    /// <summary>Make the Nth (1-based) <c>Post</c> from here on throw, once — the injection
    /// point for "something after the meeting was published failed".</summary>
    public void ThrowOnPost(int ordinal) => _context.ThrowOnPost = ordinal;

    /// <summary>Spin until <paramref name="predicate"/> holds. Bounded, and it polls a real
    /// state change rather than sleeping for an interval — the same shape as the core
    /// suite's Poll.UntilAsync.</summary>
    public static void SpinUntil(Func<bool> predicate, string what)
    {
        var elapsed = Stopwatch.StartNew();
        while (elapsed.Elapsed < CallTimeout)
        {
            if (predicate())
                return;
            Thread.Sleep(5);
        }
        throw new TimeoutException($"timed out waiting for {what}");
    }

    public void Dispose()
    {
        _work.CompleteAdding();
        _thread.Join(CallTimeout);
        _work.Dispose();
    }

    /// <summary>
    /// A SynchronizationContext that records posts instead of running them, so the test owns
    /// when the shell's UI work happens. <see cref="Send"/> is not supported: the shell only
    /// ever Posts, and a blocking Send from a thread-pool continuation into a thread that is
    /// not pumping would deadlock — better to fail loudly if that ever changes.
    /// </summary>
    private sealed class QueueingContext : SynchronizationContext
    {
        private readonly object _lock = new();
        private readonly List<(SendOrPostCallback Callback, object? State)> _posted = [];
        private int _posts;

        public int ThrowOnPost { get; set; }

        public int Pending
        {
            get { lock (_lock) return _posted.Count; }
        }

        public override void Post(SendOrPostCallback d, object? state)
        {
            lock (_lock)
            {
                if (++_posts == ThrowOnPost)
                {
                    ThrowOnPost = 0; // once only — the shell must survive one failure, not be crippled
                    throw new InvalidOperationException($"post #{_posts} failed");
                }
                _posted.Add((d, state));
            }
        }

        public override void Send(SendOrPostCallback d, object? state) =>
            throw new NotSupportedException("the tray shell must never block on the UI thread");

        public IReadOnlyList<SendOrPostCallback> Take(out object?[] states)
        {
            lock (_lock)
            {
                SendOrPostCallback[] callbacks = [.. _posted.Select(p => p.Callback)];
                states = [.. _posted.Select(p => p.State)];
                _posted.Clear();
                return callbacks;
            }
        }
    }
}
