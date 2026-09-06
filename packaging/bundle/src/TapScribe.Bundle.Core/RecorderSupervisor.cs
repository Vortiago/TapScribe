namespace TapScribe.Bundle.Core;

/// <summary>What the tray shows about the Recorder.</summary>
public enum RecorderState
{
    Preflight,
    Running,
    Stopped,
    Failed,

    /// <summary>
    /// A Recorder is answering on the port, and this tray did not start it — a
    /// <c>start.sh</c> in a terminal, or another install. Shown, never stopped: Quit takes
    /// down only what this tray owns, and ownership is recorded at SPAWN rather than
    /// configured (ADR-0022).
    /// </summary>
    Unmanaged,
}

/// <summary>
/// Runs the Bundle's two child processes: <c>tapscribe.preflight</c> to completion, then
/// the Recorder itself, with both children's stdout/stderr pumped into the tray's log.
///
/// Deliberately thin. Every decision it makes — which interpreter, which argv, which
/// environment — comes from <see cref="RecorderCommand"/> and is unit-tested there; what is
/// left here is a spawn, two event handlers, a kill, and the one classification below.
/// Reaping is not this class's job: the <see cref="IProcessReaper"/> the tray holds covers
/// the grandchildren (<c>whisperlivekit-server</c>) that this class never gets a handle to,
/// and IT is where the two platforms differ.
///
/// In Core rather than beside a shell because the lifecycle is the same on both, the way
/// the capture lifecycle already is (ADR-0022): only the reaping is platform-shaped, and
/// that is behind the seam. The spawn and the health probe are injected for the same
/// reason — so the decisions an operator meets when something is wrong are tested on the
/// Linux leg rather than by breaking a real install by hand.
/// </summary>
public sealed class RecorderSupervisor : IRecorderHost
{
    private readonly BundleLayout _layout;
    private readonly IProcessReaper? _reaper;
    private readonly Action<string> _log;
    private readonly Action<RecorderState, string> _onState;
    /// <summary>Something the operator must be told that is not a STATE — today only "the
    /// update took your model backends with it". Separate from <see cref="_onState"/>
    /// because the menu header renders a state and this is advice that outlives one.</summary>
    private readonly Action<string> _onNotice;
    private readonly Func<BundleProcess, IChildProcess> _spawn;
    private readonly Func<bool> _recorderAnswers;
    private readonly Lock _gate = new();
    private IChildProcess? _recorder;
    /// <summary>The preflight child while it runs, so Stop() can reach it too.</summary>
    private IChildProcess? _preflight;
    private bool _stopping;

    /// <param name="reaper">The crash backstop, or null for the supported degraded mode.</param>
    /// <param name="spawn">How to start one child. Defaults to a real process.</param>
    /// <param name="recorderAnswers">Whether SOMETHING is serving the Recorder's port —
    /// a <c>GET /health</c>. Consulted only when the Recorder this tray started exits
    /// immediately, which is the one moment its answer changes what the operator is
    /// told.</param>
    public RecorderSupervisor(
        BundleLayout layout,
        IProcessReaper? reaper,
        Action<string> log,
        Action<RecorderState, string> onState,
        Func<BundleProcess, IChildProcess>? spawn = null,
        Func<bool>? recorderAnswers = null,
        Action<string>? onNotice = null)
    {
        ArgumentNullException.ThrowIfNull(layout);
        ArgumentNullException.ThrowIfNull(log);
        ArgumentNullException.ThrowIfNull(onState);
        _layout = layout;
        _reaper = reaper;
        _log = log;
        _onState = onState;
        _spawn = spawn ?? (command => ChildProcess.Start(command, layout.RuntimeDirectory, log));
        _recorderAnswers = recorderAnswers ?? (() => false);
        _onNotice = onNotice ?? log;
    }

    /// <summary>Whether the Recorder currently on the port is one this tray started.
    /// Recorded at spawn, never configured: Quit stops only what this owns.</summary>
    public bool Manages
    {
        get
        {
            lock (_gate)
                return _recorder is not null;
        }
    }

    /// <summary>
    /// Boot the Recorder on a background thread; the tray keeps pumping messages. The
    /// shell ignores the returned task — that is the point of booting off-thread — and a
    /// test awaits it rather than racing the thread pool.
    ///
    /// Clearing <see cref="_stopping"/> is what makes Start-after-Stop a boot rather than a
    /// no-op: <c>Stop()</c> is the operator's <b>Stop Recorder</b> as well as Quit's teardown
    /// (ADR-0022), so the flag has to mean "this run was cancelled" and not "the tray is
    /// going away". Left latched, the next Start spawns a preflight, kills it on its own
    /// quit-race check and returns silently, wedging the menu on "Preparing TapScribe…"
    /// with both commands disabled.
    /// </summary>
    public Task Start()
    {
        lock (_gate)
            _stopping = false;
        return Task.Run(Run);
    }

    private void Run()
    {
        // TASK-BOUNDARY HANDLER — deliberately catches everything, and CodeQL's
        // cs/catch-of-all-exceptions is dismissed on it for that reason.
        //
        // Start() is fire-and-forget (Task.Run), so an exception escaping here lands in
        // an unobserved Task and vanishes: no Fail(), no log line, no state change, and a
        // tray frozen on "Preparing TapScribe…" forever with no way for the operator to
        // tell whether it is still working. Narrowing this catch would restore exactly
        // the silent death this exists to remove — an unhandled type is precisely the
        // case that must still reach the tray.
        //
        // Nothing is swallowed: the FULL exception (type, message, stack) goes to the
        // log, and only the friendly one-liner goes to the balloon. That split is the
        // substance of CodeQL's complaint about broad catches, and it is honoured here.
        try
        {
            RunCore();
        }
        catch (Exception error)
        {
            _log($"unhandled exception in supervisor: {error}");
            Fail($"TapScribe could not start: {error.Message} See the log for details.");
        }
    }

    private void RunCore()
    {
        string wheel;
        try
        {
            Directory.CreateDirectory(_layout.DataDirectory);

            // BEFORE ResolveWheel, which reads the RUNTIME's wheel folder — so on macOS
            // there is nothing to resolve until the copy has happened (ADR-0024). Here
            // rather than in the shell that has a copy to make: this method is already on
            // the background thread, already has both catch tiers, and is what BOTH Start()
            // and the operator's "Start Recorder" go through. Called from a shell instead,
            // a failed first copy could never be retried — Start Recorder would go straight
            // to ResolveWheel and answer "no TapScribe wheel found" forever.
            //
            // Costs a Windows Bundle nothing: its runtime IS its payload, so Ensure answers
            // NotNeeded without touching the disk.
            RuntimeCopyResult copied = RuntimeCopy.Ensure(_layout, _log);
            if (copied.BackendsLost)
                _onNotice(
                    "TapScribe was updated, so the speech models you installed are gone. "
                        + "Open the dashboard and run Setup again to reinstall them.");

            wheel = _layout.ResolveWheel();
        }
        catch (Exception error) when (error is BundleLayoutException or IOException or UnauthorizedAccessException)
        {
            Fail(error.Message);
            return;
        }

        // Runtime, not payload: this is the interpreter pip will target, which on macOS is
        // the copy rather than what shipped (ADR-0024). A log naming the .app would send
        // whoever reads it to a folder nothing writes to.
        _log($"runtime dir: {_layout.RuntimeDirectory}");
        _log($"data dir:    {_layout.DataDirectory}");
        _log($"wheel:       {wheel}");

        _onState(RecorderState.Preflight, "Preparing TapScribe…");
        if (!RunPreflight(wheel))
            return;

        // Preflight blocks on an unbounded pip install that can pull torch — minutes,
        // gigabytes. If the operator hit Quit during it, Stop() already ran and found no
        // Recorder to kill, so spawning one now would start a process nobody is left to
        // reap. The reaper usually covers it, but a null one is an explicitly supported
        // degraded path, and on THAT path the Recorder plus its WhisperLiveKit grandchild
        // would be orphaned holding port 8001 — the exact leak this class exists to
        // prevent.
        lock (_gate)
        {
            if (_stopping)
                return;
        }

        StartRecorder(wheel);
    }

    /// <summary>
    /// Blocking, logged. A non-zero exit is reported but does NOT stop the Recorder:
    /// preflight's steps are repairs (the CUDA torch swap, the model fetch), and a failed
    /// repair still leaves a Recorder that boots — with a degraded backend the operator can
    /// see in the log and fix from /setup.
    /// </summary>
    private bool RunPreflight(string wheel)
    {
        BundleProcess command = RecorderCommand.Preflight(_layout, wheel);
        try
        {
            using IChildProcess process = _spawn(command);
            bool quitRaced;
            lock (_gate)
            {
                quitRaced = _stopping;
                if (!quitRaced)
                    _preflight = process;
            }

            if (quitRaced)
            {
                // Quit landed between RunCore's check and this spawn. Kill OUTSIDE the
                // lock — Stop() takes the same lock, and blocking it behind a kill is
                // the opposite of what Quit is trying to achieve.
                process.Kill();
                return false;
            }

            try
            {
                process.WaitForExit();
            }
            finally
            {
                lock (_gate)
                    _preflight = null;
            }
            if (process.ExitCode != 0)
                _log($"preflight exited {process.ExitCode} — continuing; some optional components may be missing.");
            return true;
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            // Could not launch the interpreter at all — a broken install, not a failed
            // repair. There is nothing to start after this.
            Fail($"Could not run {command.Executable}: {error.Message}");
            return false;
        }
    }

    private void StartRecorder(string wheel)
    {
        BundleProcess command = RecorderCommand.Recorder(_layout, wheel);
        IChildProcess process;

        // Only the SPAWN is "could not start the Recorder". Anything after it throws with
        // the child alive and `_recorder` published, and reporting Failed there would offer
        // Start for a Recorder that is running and refuse Stop for one this tray owns —
        // a second Start would then spawn a sibling and orphan the first. Those reach Run()'s
        // catch-all instead, which logs the full exception.
        try
        {
            process = _spawn(command);
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            Fail($"Could not start the Recorder ({command.Executable}): {error.Message}");
            return;
        }

        // The same quit-race check RunPreflight makes at its own spawn, and for the same
        // reason: Stop() reads `_recorder` under this lock, so a Quit landing between
        // RunCore's check and this publish would have found nothing to kill and the child
        // spawned a moment later would be reached by nobody. The reaper usually covers it,
        // but a null reaper is an explicitly supported degraded path, and on THAT path the
        // Recorder plus its WhisperLiveKit grandchild are orphaned holding port 8001 — the
        // exact leak this class exists to prevent.
        bool quitRaced;
        lock (_gate)
        {
            quitRaced = _stopping;
            if (!quitRaced)
                _recorder = process;
        }

        if (quitRaced)
        {
            // Killed OUTSIDE the lock, like the preflight twin: Stop() takes the same lock
            // and must not block behind a kill. Disposed here because nothing else holds a
            // handle to it — `_recorder` was never published.
            try
            {
                if (!process.HasExited)
                    process.Kill();
            }
            catch (Exception error) when (error is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
            {
                // Already gone, or exited between HasExited and Kill. Nothing to do.
                _log($"start (quit raced the spawn): {error.Message}");
            }
            process.Dispose();
            return;
        }

        Enrol(process);

        // Running is published BEFORE the subscription, and that order is load-bearing: the
        // seam sets EnableRaisingEvents inside the `add`, which for a child that has ALREADY
        // exited raises Exited synchronously. Reporting Running afterwards would overwrite
        // the Unmanaged/Stopped verdict OnRecorderExited just produced, leaving the tray
        // green and "running" over a dead dashboard — the exact failure this ordering is
        // meant to prevent. Process latches the exit, so a subscription that lands after the
        // child is gone still fires.
        _onState(RecorderState.Running, "TapScribe is running.");
        process.Exited += (_, _) => OnRecorderExited(process);
    }

    /// <summary>
    /// The Recorder this tray started has gone. WHY decides what the operator is told, and
    /// the discriminator is the port, not the exit code (ADR-0022): unmanaged is decided by
    /// the SPAWN ATTEMPT, not by probing first.
    ///
    ///   - something still answers /health ⇒ somebody else's Recorder holds the port. Shown
    ///     as running-but-unmanaged, and Quit will not touch it.
    ///   - nothing answers ⇒ this install is broken. Reported as failed rather than adopted,
    ///     so a crash-loop looks like a crash-loop.
    ///
    /// Deliberately NOT read out of the child's stdout: matching an "address already in
    /// use" string means owning uvicorn's wording forever, and the probe answers the
    /// question directly.
    /// </summary>
    private void OnRecorderExited(IChildProcess process)
    {
        lock (_gate)
        {
            // A superseded child says nothing about the Recorder the tray now holds: Stop
            // owns disposal on the quitting path, so only the identity case falls through.
            if (_stopping || !ReferenceEquals(_recorder, process))
                return;
            // Ownership ends here: whatever is on the port now, this tray no longer has a
            // handle to it, so Quit must not try to kill it.
            _recorder = null;
        }

        // Read the code BEFORE disposing — ExitCode throws once the Process is closed. Stop()
        // disposes the handle it takes; this is the other path, and without it a crash-loop
        // strands one undisposed child per cycle.
        int code = process.ExitCode;
        process.Dispose();
        _log($"recorder exited with code {code}");

        if (_recorderAnswers())
        {
            _log("something is still serving the Recorder's port — not ours to stop.");
            _onState(
                RecorderState.Unmanaged,
                "TapScribe is already running from somewhere else. This tray will not stop it.");
            return;
        }

        _onState(
            RecorderState.Stopped,
            $"TapScribe stopped unexpectedly (exit {code}). See the log.");
    }

    private void Enrol(IChildProcess process)
    {
        // Only when the tray could NOT put ITSELF in the reaper: otherwise the child is
        // already a member by inheritance and a second enrolment fails.
        if (_reaper is { CoversChildrenByInheritance: false } reaper && !reaper.Adopt(process))
            _log("reaper: could not enrol the child — a crash may leave whisperlivekit-server running.");
    }

    /// <summary>
    /// Ask the Recorder to go away, then let the reaper take the rest. The kill is
    /// whole-tree for the ordinary case; the reaper is the backstop for the case this
    /// misses (a grandchild that re-parented).
    ///
    /// Stops only a Recorder THIS tray started: <see cref="_recorder"/> is null for an
    /// unmanaged one, so a Recorder the operator launched from a terminal outlives Quit.
    /// </summary>
    public void Stop()
    {
        IChildProcess? handle;
        IChildProcess? preflight;
        lock (_gate)
        {
            _stopping = true;
            handle = _recorder;
            _recorder = null;
            preflight = _preflight;
        }

        // Preflight is a long blocking pip install; killing it is what makes Quit feel
        // immediate instead of "nothing happens for four minutes". Not disposed here —
        // RunPreflight's own `using` owns it.
        if (preflight is not null)
        {
            try
            {
                if (!preflight.HasExited)
                    preflight.Kill();
            }
            catch (Exception error) when (error is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
            {
                _log($"stop (preflight): {error.Message}");
            }
        }

        if (handle is null)
            return;

        // `using` rather than try/finally + Dispose(): same guarantee, and it keeps
        // CodeQL's cs/missed-using-statement clean rather than needing a suppression.
        using IChildProcess process = handle;
        try
        {
            if (!process.HasExited)
            {
                process.Kill();
                process.WaitForExit(5000);
            }
        }
        catch (Exception error) when (error is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
        {
            // Already gone, or exited between HasExited and Kill. Nothing to do — and the
            // reaper takes down anything still alive when we exit, which is exactly the
            // leak this Stop() is trying to avoid.
            _log($"stop: {error.Message}");
        }
    }

    private void Fail(string message)
    {
        // A Quit mid-preflight kills the child, which surfaces here as a spawn/wait
        // failure — but the operator asked for that, so a red "TapScribe could not
        // start" balloon on the way out would be a lie. Log it and stay quiet.
        lock (_gate)
        {
            if (_stopping)
            {
                _log($"(after quit) {message}");
                return;
            }
        }

        _log($"FATAL: {message}");
        _onState(RecorderState.Failed, message);
    }

    public void Dispose() => Stop();
}
