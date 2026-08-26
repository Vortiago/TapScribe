using System.Collections.Concurrent;
using System.Runtime.ExceptionServices;

namespace TapScribe.TrayBridge.Windows.Tests;

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
/// thread takes the whole host down with no assertion and no stack. <see cref="Run"/> is the
/// only producer of work, and every item it queues catches everything and hands it back to
/// the waiting caller — so the thread's own frame is unreachable by construction, with no
/// second mechanism to keep in step.</para>
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
    /// <summary>Bound on anything this harness or a test using it waits for. Generous — it is
    /// a backstop against a hang, not a timing assertion, and one value so a test cannot wait
    /// longer for the shell than the harness will wait for the thread.</summary>
    public static readonly TimeSpan CallTimeout = TimeSpan.FromSeconds(30);

    private readonly BlockingCollection<Action> _work = new();
    private readonly QueueingContext _context = new();
    private readonly TaskCompletionSource _ready = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Thread _thread;

    // The shell this harness built, released on the STA thread at teardown. One per harness:
    // a test drives exactly one tray.
    private TrayContext? _built;

    public StaShell()
    {
        _thread = new Thread(Loop) { IsBackground = true, Name = "tray-shell-sta" };
        _thread.SetApartmentState(ApartmentState.STA);
        _thread.Start();
        // The ONLY thing the constructor does with the thread: wait until it has installed
        // its SynchronizationContext. No method of this class is called from here (rule 1).
        if (!_ready.Task.Wait(CallTimeout))
            throw new TimeoutException("the STA shell thread never signalled ready");
    }

    private void Loop()
    {
        // The shell reads SynchronizationContext.Current once, on the message loop's first
        // turn, exactly as it reads the WinForms one in production, and wraps it in the one
        // dispatcher its runtime marshals through. Installed here, by the thread that owns it,
        // rather than marshalled in.
        SynchronizationContext.SetSynchronizationContext(_context);
        _ready.SetResult(); // publishes the line above to the constructor's thread

        foreach (Action work in _work.GetConsumingEnumerable())
            work(); // Marshal wraps every item; nothing can escape to this frame (rule 2)
    }

    /// <summary>Run <paramref name="action"/> on the STA thread and wait for it, rethrowing
    /// whatever it threw with its original stack — so a test reads like a direct call. This
    /// is also what holds rule 2: the queued item catches everything, so nothing can reach
    /// the thread's own frame and take the host down with it.</summary>
    public void Run(Action action)
    {
        ArgumentNullException.ThrowIfNull(action);
        // Completed by the queued item below. A TaskCompletionSource rather than a
        // ManualResetEventSlim on purpose: the event was deliberately never disposed, to dodge
        // a Set()-after-Dispose race on a timed-out call — sound reasoning about the wrong
        // tool, since a source with nothing to release cannot have that race in the first
        // place. (It was NOT about handle allocation: ManualResetEventSlim only materialises
        // a kernel handle if someone reads .WaitHandle, which nothing here did.)
        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        ExceptionDispatchInfo? failure = null;
        _work.Add(() =>
        {
            try
            {
                action();
            }
            catch (Exception ex)
            {
                // Deliberately unfiltered, and it swallows NOTHING: the exception is captured
                // here and rethrown on the caller's thread below with its original stack. It
                // has to be unfiltered because it stands between a test's arbitrary code and a
                // background thread whose escapes kill the whole test host with no assertion
                // and no stack (rule 2) — a filter would be a list of the failures allowed to
                // be reported, which is exactly backwards.
                failure = ExceptionDispatchInfo.Capture(ex);
            }
            finally
            {
                done.SetResult();
            }
        });
        if (!done.Task.Wait(CallTimeout))
            throw new TimeoutException("the STA shell thread did not finish the call in time");
        failure?.Throw();
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

    /// <summary>
    /// Build the tray shell on the STA thread, give it the message loop's first turn, and take
    /// responsibility for releasing it there, INCLUDING when the test fails. WinForms objects
    /// built on this apartment must not be left to a finalizer that runs after the apartment is
    /// gone, and a test that asserts its way out early would otherwise leave exactly that
    /// behind.
    ///
    /// Running the loop-start kick is not a convenience: the shell builds its runtime there,
    /// because that is the first moment SynchronizationContext.Current is the one every
    /// marshalled callback goes through. A shell that never got that turn has no runtime, which
    /// is the state the scheduling test is about and no other test wants.
    /// </summary>
    /// <param name="dependencies">Overrides the harness's own, for a test that varies one port
    /// of the outside world and still wants the STA construction and the release-on-failure
    /// that come with building through here. Its kicks are then the caller's to run: only the
    /// harness's are known here.</param>
    public TrayContext Build(TrayHarness harness, TrayDependencies? dependencies = null)
    {
        ArgumentNullException.ThrowIfNull(harness);
        _built = Get(() => new TrayContext(harness.Settings, dependencies ?? harness.Dependencies));
        foreach (Action kick in harness.Kicks)
            Run(kick.Invoke);
        return _built;
    }

    /// <summary>Quit the way the operator's click does, and let the teardown land: the runtime
    /// tears the meeting down and then marshals <c>Shutdown</c> BACK to this thread, so the
    /// shell releases its UI on a drained callback rather than inside the quit itself.</summary>
    public void Quit(TrayContext tray)
    {
        ArgumentNullException.ThrowIfNull(tray);
        Task quit = Get(tray.QuitAsync);
        Assert.True(quit.Wait(CallTimeout), "the quit never settled");
        _ = Drain();
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
                IReadOnlyList<(SendOrPostCallback Callback, object? State)> batch = _context.Take();
                if (batch.Count == 0)
                    return;
                foreach ((SendOrPostCallback callback, object? state) in batch)
                {
                    try
                    {
                        callback(state);
                    }
                    catch (Exception ex)
                    {
                        // Also unfiltered, also swallows nothing: these are the SHELL's posted
                        // callbacks, arbitrary by definition, and they are handed back to the
                        // caller in the returned list. A callback running after Quit touches
                        // components Quit disposed — which in production never happens, since
                        // the message loop is gone by then — so it must not fail the test, but
                        // it must still be visible to one that cares.
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

    public void Dispose()
    {
        TrayContext? built = _built;
        _built = null;
        if (built is not null)
        {
            try
            {
                Run(built.Dispose);
            }
            catch (Exception ex) when (ex is TimeoutException or ObjectDisposedException)
            {
                // This runs from a using-block's dispose, where a test may already be
                // reporting its own failure — so a wedged or already-torn-down shell must not
                // throw over the top of it and replace the thing the reader needs to see.
                // What is lost is this teardown error; the objects go unreleased, which the
                // process exit resolves anyway. Nothing else can reach here: Run rethrows only
                // what the disposal itself threw, and TrayContext.Dispose is idempotent.
            }
        }

        _work.CompleteAdding();
        // Join before the process (or the next test) moves on: WinForms objects built on this
        // apartment must not be left to a finalizer running after the apartment is gone.
        _thread.Join(CallTimeout);
        // Deliberately NOT disposing _work: the loop may still be unwinding, and it holds no
        // unmanaged resource worth the race.
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

        public int Pending
        {
            get { lock (_lock) return _posted.Count; }
        }

        public override void Post(SendOrPostCallback d, object? state)
        {
            lock (_lock)
                _posted.Add((d, state));
        }

        public override void Send(SendOrPostCallback d, object? state) =>
            throw new NotSupportedException("the tray shell must never block on the UI thread");

        public IReadOnlyList<(SendOrPostCallback Callback, object? State)> Take()
        {
            lock (_lock)
            {
                (SendOrPostCallback, object?)[] batch = [.. _posted];
                _posted.Clear();
                return batch;
            }
        }
    }
}
