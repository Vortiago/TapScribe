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
/// What it deliberately does NOT do, all of it learned from the run that aborted this
/// assembly before its first result: no notification-area icon is registered (the shell's OS
/// surface is substituted — see <see cref="ITrayIndicator"/>), no native timer window is
/// created, no window is shown, no message loop is entered, nothing WinForms is left for a
/// finalizer to reach after this apartment is gone (see <see cref="Build"/>), no exception
/// may escape the thread, and every wait is bounded — so a wedged test fails rather than
/// hanging the job.
/// </summary>
internal sealed class StaShell : IDisposable
{
    /// <summary>Bound on any single marshalled call. Generous — it is a backstop against a
    /// hang, not a timing assertion.</summary>
    private static readonly TimeSpan CallTimeout = TimeSpan.FromSeconds(30);

    private readonly BlockingCollection<Action> _work = new();
    private readonly QueueingContext _context = new();
    private readonly Thread _thread;

    /// <summary>Why WinForms components can't be built on this host, or null if they can.</summary>
    private readonly string? _unavailable;

    public StaShell()
    {
        _thread = new Thread(Loop) { IsBackground = true, Name = "tray-shell-sta" };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        // The shell reads SynchronizationContext.Current when Start/End/OpenPastMeeting run,
        // exactly as it reads the WinForms one in production.
        Run(() => SynchronizationContext.SetSynchronizationContext(_context));
        _unavailable = Run(Probe);
    }

    /// <summary>
    /// The work loop. Nothing may escape it: an unhandled exception on a background thread
    /// takes the whole test host down with no assertion and no stack — which is exactly the
    /// failure mode this project has already produced once, and the one thing a test harness
    /// must never be able to cause. Every escape is turned into a recorded failure instead.
    /// </summary>
    private void Loop()
    {
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
                    // Run() already funnels test failures back to the caller, so reaching
                    // here means the harness itself broke (a signal disposed under a timed-out
                    // call, say). Record it for the next Run to surface; what is lost is the
                    // ordering, which is worth losing to keep the host alive.
                    lock (_work)
                        _harnessFailures.Add(ex);
                }
            }
        }
        catch (Exception ex)
        {
            lock (_work)
                _harnessFailures.Add(ex);
        }
    }

    private readonly List<Exception> _harnessFailures = [];

    // Anything the STA thread failed at outside a marshalled call. Surfaced by the next Run
    // rather than swallowed: once the harness itself has broken, every result after it is
    // suspect, and reporting at the first opportunity can't mask a real failure because the
    // harness failure IS the first one.
    private Exception? TakeHarnessFailure()
    {
        lock (_work)
        {
            if (_harnessFailures.Count == 0)
                return null;
            var failure = new AggregateException("the STA test harness failed", _harnessFailures);
            _harnessFailures.Clear();
            return failure;
        }
    }

    /// <summary>
    /// Fail the calling test with a reason when this host cannot build WinForms components at
    /// all, rather than letting it fail somewhere deeper and less legibly. A named
    /// environmental failure is a result someone can act on; an aborted run is not. (A SKIP
    /// would be better still, but xunit 2.9's Assert has no dynamic skip and this repo takes
    /// no new test dependency for one.)
    /// </summary>
    public void RequireWinForms()
    {
        if (_unavailable is not null)
            Assert.Fail(
                $"WinForms components cannot be created on this host: {_unavailable}. " +
                "This is an environment failure, not a behaviour failure — the tray shell's " +
                "logic is unreachable here and these tests need a Windows desktop session.");
    }

    // Build (and release) the two component types every tray test needs, on the STA thread,
    // before any test touches the shell.
    private static string? Probe()
    {
        try
        {
            using var strip = new ContextMenuStrip();
            using var item = new ToolStripMenuItem("probe");
            strip.Items.Add(item);
            return null;
        }
        catch (Exception ex)
        {
            // Deliberately broad: the point is to convert ANY environmental failure into one
            // named message, so a runner without a desktop reports why instead of failing
            // every test with the same opaque error somewhere deeper.
            return $"{ex.GetType().Name}: {ex.Message}";
        }
    }

    /// <summary>Run <paramref name="action"/> on the STA thread and wait for it, rethrowing
    /// whatever it threw with its original stack — so a test reads like a direct call.</summary>
    public void Run(Action action)
    {
        if (TakeHarnessFailure() is { } broken)
            throw broken;

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

    public T Run<T>(Func<T> function)
    {
        T result = default!;
        Run(() => result = function());
        return result;
    }

    private readonly List<IDisposable> _owned = [];

    /// <summary>
    /// Build the tray shell on the STA thread and take responsibility for releasing it there
    /// — including when the test FAILS. WinForms objects built on this apartment must not be
    /// left to a finalizer that runs after the apartment is gone, and a test that asserts its
    /// way out early would otherwise leave exactly that behind.
    /// </summary>
    public TrayContext Build(TrayHarness harness)
    {
        ArgumentNullException.ThrowIfNull(harness);
        TrayContext tray = Run(() => new TrayContext(harness.Settings, harness.Dependencies));
        lock (_work)
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
        lock (_work)
        {
            owned = [.. _owned];
            _owned.Clear();
        }
        if (owned.Length > 0)
            Run(() =>
            {
                foreach (IDisposable disposable in owned)
                {
                    try
                    {
                        disposable.Dispose();
                    }
                    catch (Exception ex)
                    {
                        // Teardown of an already-failing test. Recorded, never rethrown: a
                        // throw here would replace the test's real failure with this one, and
                        // the remaining objects would go unreleased. What is lost is a
                        // secondary error on a path that is already reporting a primary one.
                        lock (_work)
                            _harnessFailures.Add(ex);
                    }
                }
            });

        _work.CompleteAdding();
        // Join before the process (or the next test) moves on: WinForms objects built on this
        // apartment must not be left to a finalizer running after the apartment is gone.
        _thread.Join(CallTimeout);
        // Deliberately NOT disposing _work: the loop above locks on it to record harness
        // failures, and disposing a BlockingCollection whose consumer may not have unwound
        // yet buys nothing — it holds no unmanaged resource.
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
