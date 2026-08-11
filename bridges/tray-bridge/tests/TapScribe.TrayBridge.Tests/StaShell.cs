using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.ExceptionServices;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// A dedicated STA thread with a hand-pumped <see cref="SynchronizationContext"/> — the
/// harness every tray test drives <c>TrayContext</c> through.
///
/// Two reasons it exists. WinForms types are STA-affine and xunit v2 runs its tests on MTA
/// thread-pool threads, so a <c>ContextMenuStrip</c> built on the test thread is built on the
/// wrong kind of thread. And the shell marshals every callback through
/// <c>SynchronizationContext.Post</c>, which in production a WinForms message loop pumps — a
/// real loop here would mean <c>Application.Run</c> (which blocks, and would hang CI) and
/// non-deterministic timing. So Post QUEUES, and a test calls <see cref="Drain"/> when it
/// wants the shell's pending UI work to happen. That makes "has the tray updated yet?" a
/// decision the test makes rather than a race it runs.
///
/// <para><b>Two rules this file exists to hold, both written in blood.</b></para>
///
/// <para>1. <b>The constructor calls no method of this class, and no method of this class
/// reaches the constructor.</b> Initialisation happens on the STA thread inside
/// <see cref="Loop"/> before the work loop starts; the constructor only waits for a signal.
/// An earlier version marshalled its setup through the public <c>Run</c> — which had a
/// generic overload that recursed into itself — and every construction overflowed the stack
/// and killed the test host. There is no overload set here any more (<see cref="Run"/> takes
/// an <see cref="Action"/>, <see cref="Get{T}"/> takes a <see cref="Func{T}"/>), so a lambda
/// can no longer resolve to the wrong one of the two.</para>
///
/// <para>2. <b>Nothing may escape the STA thread.</b> An unhandled exception on a background
/// thread takes the whole host down with no assertion and no stack. Every escape becomes a
/// recorded failure that the next call surfaces.</para>
///
/// Beyond that it deliberately does nothing that needs a desktop: no notification-area icon
/// is registered (the shell's OS surface is substituted — see <see cref="ITrayIndicator"/>),
/// no native timer window is created, no window is shown, no message loop is entered,
/// nothing WinForms is left for a finalizer to reach after this apartment is gone (see
/// <see cref="Build"/>), and every wait is bounded — so a wedged test fails rather than
/// hanging the job.
/// </summary>
internal sealed class StaShell : IDisposable
{
    /// <summary>Bound on any single marshalled call. Generous — it is a backstop against a
    /// hang, not a timing assertion.</summary>
    private static readonly TimeSpan CallTimeout = TimeSpan.FromSeconds(30);

    private readonly object _sync = new();
    private readonly BlockingCollection<Action> _work = new();
    private readonly QueueingContext _context = new();
    private readonly ManualResetEventSlim _ready = new(false);
    private readonly List<Exception> _harnessFailures = [];
    private readonly List<IDisposable> _owned = [];
    private readonly Thread _thread;

    public StaShell()
    {
        _thread = new Thread(Loop) { IsBackground = true, Name = "tray-shell-sta" };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        // The ONLY thing the constructor does with the thread: wait until it has installed
        // its SynchronizationContext. No method of this class is called from here (rule 1).
        if (!_ready.Wait(CallTimeout))
            throw new TimeoutException("the STA shell thread never signalled ready");
    }

    private void Loop()
    {
        try
        {
            // The shell reads SynchronizationContext.Current when Start / End /
            // OpenPastMeeting run, exactly as it reads the WinForms one in production.
            // Installed here, by the thread that owns it, rather than marshalled in.
            SynchronizationContext.SetSynchronizationContext(_context);
        }
        catch (Exception ex)
        {
            Record(ex);
        }
        finally
        {
            _ready.Set(); // publishes the line above to the constructor's thread
        }

        try
        {
            foreach (Action work in _work.GetConsumingEnumerable())
            {
                try
                {
                    work();
                }
                catch (Exception ex)
                {
                    // Marshal() already funnels a test's own failures back to its caller, so
                    // reaching here means the harness itself broke. Record it for the next
                    // call to surface; what is lost is the ordering, which is worth losing to
                    // keep the host alive (rule 2).
                    Record(ex);
                }
            }
        }
        catch (Exception ex)
        {
            Record(ex);
        }
    }

    private void Record(Exception failure)
    {
        lock (_sync)
            _harnessFailures.Add(failure);
    }

    // Surfaced by the next call rather than swallowed: once the harness itself has broken,
    // every result after it is suspect, and reporting at the first opportunity cannot mask a
    // real failure because the harness failure IS the first one.
    private Exception? TakeHarnessFailure()
    {
        lock (_sync)
        {
            if (_harnessFailures.Count == 0)
                return null;
            var failure = new AggregateException("the STA test harness failed", _harnessFailures);
            _harnessFailures.Clear();
            return failure;
        }
    }

    /// <summary>Run <paramref name="action"/> on the STA thread and wait for it, rethrowing
    /// whatever it threw with its original stack — so a test reads like a direct call.</summary>
    public void Run(Action action)
    {
        if (TakeHarnessFailure() is { } broken)
            throw broken;
        Marshal(action);
    }

    /// <summary>
    /// <see cref="Run"/>, for work that produces a value. A SEPARATE NAME, not an overload:
    /// as an overload, a lambda whose body is an expression with a value (<c>() => x = f()</c>)
    /// binds to the <see cref="Func{T}"/> form in preference to the <see cref="Action"/> one,
    /// so the generic method called itself. Two names make that unwritable.
    /// </summary>
    public T Get<T>(Func<T> function)
    {
        ArgumentNullException.ThrowIfNull(function);
        T result = default!;
        Run(() => { result = function(); }); // statement body: no value, nothing to mis-bind
        return result;
    }

    // The marshalling itself, with no harness-failure check, so teardown can use it without
    // replacing a test's real failure with a stale one.
    private void Marshal(Action action)
    {
        ArgumentNullException.ThrowIfNull(action);
        ExceptionDispatchInfo? failure = null;
        // Deliberately NOT disposed: a call that times out below leaves the STA thread still
        // holding this, and Set() on a disposed signal throws from a thread whose escapes kill
        // the host. Letting the GC take it costs nothing and removes the race entirely.
        var done = new ManualResetEventSlim(false);
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

    /// <summary>
    /// Build the tray shell on the STA thread and take responsibility for releasing it there
    /// — including when the test FAILS. WinForms objects built on this apartment must not be
    /// left to a finalizer that runs after the apartment is gone, and a test that asserts its
    /// way out early would otherwise leave exactly that behind.
    /// </summary>
    public TrayContext Build(TrayHarness harness)
    {
        ArgumentNullException.ThrowIfNull(harness);
        TrayContext tray = Get(() => new TrayContext(harness.Settings, harness.Dependencies));
        lock (_sync)
            _owned.Add(tray);
        return tray;
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
        ArgumentNullException.ThrowIfNull(predicate);
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
        IDisposable[] owned;
        lock (_sync)
        {
            owned = [.. _owned];
            _owned.Clear();
        }

        if (owned.Length > 0)
        {
            try
            {
                // Marshal, not Run: a pending harness failure must not be thrown from a
                // using-block's dispose, where it would replace the test's real failure.
                Marshal(() =>
                {
                    foreach (IDisposable disposable in owned)
                    {
                        try
                        {
                            disposable.Dispose();
                        }
                        catch (Exception ex)
                        {
                            Record(ex);
                        }
                    }
                });
            }
            catch (TimeoutException ex)
            {
                // The STA thread is wedged on an earlier call. Recorded rather than thrown for
                // the same reason: this runs while a test may already be reporting a failure.
                // What is lost is the teardown error; the objects go unreleased, which the
                // process exit resolves anyway.
                Record(ex);
            }
        }

        _work.CompleteAdding();
        // Join before the process (or the next test) moves on: WinForms objects built on this
        // apartment must not be left to a finalizer running after the apartment is gone.
        _thread.Join(CallTimeout);
        // Deliberately NOT disposing _work or _ready: the loop may still be unwinding, and
        // neither holds an unmanaged resource worth the race.
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
