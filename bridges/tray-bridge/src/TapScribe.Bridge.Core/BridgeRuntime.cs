using System.Linq;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The meeting lifecycle, with no UI framework in it: Start resolves the operator's device
/// selection against the devices present now, mints a detached session on the Recorder, and
/// runs one capture pipeline per resolved device: all co-located in that one session so both
/// sides of a meeting are recorded as separately-attributed speakers.
///
/// Everything the operator would see goes through <see cref="ITrayView"/>, and every touch of
/// it is marshalled through <see cref="IDispatcher"/>. That pairing is the whole extraction:
/// the WinForms tray and the AppKit menu bar are two implementations of those two seams over
/// one tested lifecycle, rather than two copies of the lifecycle.
/// </summary>
public sealed class BridgeRuntime
{
    private readonly ITrayView _view;
    private readonly IDispatcher _dispatcher;
    private readonly BridgeDependencies _deps;
    private readonly object _gate = new();

    // Read and written under _gate, always: Start snapshots them and carries the snapshot into
    // thread-pool continuations, so there is no thread any of these is private to.
    private BridgeSettings _settings;
    private Meeting? _meeting;                   // non-null exactly while a meeting is streaming
    private Task? _startTask;

    private Task? _endTask;
    // The in-flight End/Resume flow's cancellation, published so teardown can stop it and
    // cleared when the flow ends. Never disposed: see the construction site for why that is
    // the simplification and not the leak.
    private CancellationTokenSource? _flowCancellation;
    // Set once teardown has claimed the runtime. A start mid-flight then tears its own meeting
    // down instead of publishing it into a shell that has already stopped.
    private bool _quitting;

    private TrayStatus? _lastStatus;             // what the view is currently showing

    private readonly RuntimeBudgets _budgets;

    public BridgeRuntime(
        ITrayView view, IDispatcher dispatcher, BridgeDependencies dependencies, BridgeSettings settings,
        RuntimeBudgets? budgets = null)
    {
        ArgumentNullException.ThrowIfNull(view);
        ArgumentNullException.ThrowIfNull(dispatcher);
        ArgumentNullException.ThrowIfNull(dependencies);
        ArgumentNullException.ThrowIfNull(settings);
        _view = view;
        _dispatcher = dispatcher;
        _deps = dependencies;
        _settings = settings;
        _budgets = budgets ?? new RuntimeBudgets();
    }

    /// <summary>The in-flight Start, so a test can await the real one instead of polling.</summary>
    public Task? StartTask
    {
        get { lock (_gate) return _startTask; }
    }

    /// <summary>
    /// Begin a meeting. Returns as soon as the work is scheduled: the mint is a network
    /// round-trip, so the rest runs on continuations and reports through the view.
    /// </summary>
    public void Start()
    {
        BridgeSettings settings;
        lock (_gate)
        {
            // Already running, already starting (the mint is a round-trip long), or on the
            // way out.
            if (_meeting is not null || _startTask is { IsCompleted: false } || _quitting)
                return;
            settings = _settings;
        }

        // Disable Start now so a second click can't race a second meeting; the rest is async
        // and reports back through the dispatcher.
        ShowBusy();
        ApplyStatus(new TrayStatus.Starting());

        // Publish the task rather than firing and forgetting: teardown waits on it, so a
        // meeting minted a moment before the operator quit is torn down instead of abandoned.
        // Safe to assign after the call: StartAsync yields at the first await.
        Task start = StartAsync(settings);
        lock (_gate)
            _startTask = start;
    }

    private async Task StartAsync(BridgeSettings settings)
    {
        IAudioDeviceEnumerator? enumerator = null;
        try
        {
            // 1) Resolve the operator's selection against what is present RIGHT NOW
            //    (follow-default binds to the current default). The base identity goes in so
            //    the collision check runs on the identity each device will actually tap under:
            //    a blank Speaker ID streams under the base one, so two devices can be distinct
            //    here and the same speaker at the Recorder. A non-Ok verdict is a hard stop
            //    surfaced BEFORE any network call or device open.
            enumerator = _deps.OpenEnumerator();
            TapConnectionOptions baseOptions = settings.ToConnectionOptions();
            ResolveResult resolution = DeviceSelection.Resolve(
                settings.EffectiveDevices, enumerator.List(), baseOptions.Identity);
            if (resolution.Verdict != SelectionVerdict.Ok)
            {
                FailToIdle("Could not start meeting", DescribeVerdict(resolution.Verdict));
                return; // the finally releases the enumerator on this early exit
            }

            // 2) Mint a detached session. This doubles as the connection pre-flight: an
            //    unreachable Recorder or a rejected token throws here, before any device is
            //    opened.
            string sessionId;
            using (var cts = new CancellationTokenSource(_budgets.MintTimeout))
                sessionId = await _deps.MintDetachedSession(settings, cts.Token).ConfigureAwait(false);

            // 3) One tap per resolved device, each routing into the one session under its own
            //    identity/name and its OWN gate (#151). ToTapOptions preserves the Resolved
            //    order, so options[i] pairs with Resolved[i].
            IReadOnlyList<TapConnectionOptions> perDevice = resolution.ToTapOptions(sessionId, baseOptions);
            var specs = new List<PipelineSpec>();
            for (int i = 0; i < resolution.Resolved.Count; i++)
            {
                ResolvedDevice resolved = resolution.Resolved[i];
                TryAddSpec(specs, enumerator, resolved, perDevice[i]);
            }
            if (specs.Count == 0)
                // Every resolved device failed to OPEN (in use, format unsupported, gone).
                // StartAll would refuse an empty set anyway, so this is kept for its VERB:
                // opening is this runtime's own stage and the orchestrator cannot name it, and
                // "opened" versus "started" is the operator's only clue which of the two stages
                // their devices died at.
                throw new InvalidOperationException("No selected device could be opened.");

            // Which devices are actually streaming, and what that means for the status line, is
            // the core's DeviceTally. Touched from the dispatcher only (both callbacks marshal
            // first), which is the tally's contract.
            var tally = new DeviceTally(specs.Count);
            // Ownership of the enumerator transfers AT THE CALL, exactly as the specs' does:
            // StartAll releases everything it was given on every exit that is not a handed-back
            // orchestrator, so the local is cleared first and the finally below has nothing
            // left to release on the paths that throw from inside it.
            IAudioDeviceEnumerator opening = enumerator;
            enumerator = null;
            CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
                specs,
                onConnected: id => _dispatcher.Post(() => ApplyStatus(tally.Connected(id).Status)),
                onFailed: (id, ex) => _dispatcher.Post(() =>
                {
                    TallyReport report = tally.Dropped(id);
                    ApplyStatus(report.Status);
                    // Only on a transition: a tap that cannot reach the Recorder reports once
                    // per Utterance, so a device that dropped ONCE would otherwise notify for
                    // the whole meeting.
                    if (report.Transition)
                        _view.ShowNotice($"{id} stopped", ex.Message, NoticeKind.Warning);
                }),
                opening);
                // No shared gate arg: each spec already carries its own per-device gate.

            bool abandoned;
            lock (_gate)
            {
                // Teardown ran while this start was in flight. Publishing now would hand the
                // meeting to a shell that has already stopped: nobody would ever dispose it,
                // the captures would keep streaming, and the detached session would stay open
                // on the Recorder. Take the teardown ourselves instead. This is the same lock
                // TakeMeeting uses, so exactly one of the two runs it.
                abandoned = _quitting;
                if (!abandoned)
                    // DateTimeOffset.Now is the meeting's wall-clock start, for the
                    // Past-meetings history (#168).
                    _meeting = new Meeting(orchestrator, sessionId, DateTimeOffset.Now);
            }
            if (abandoned)
            {
                // The bounded teardown, the same one QuitAsync uses, which releases the
                // enumerator after the captures it opened.
                await orchestrator.DisposeAsync().ConfigureAwait(false);
                return;
            }

            // Devices that did not resolve are a non-fatal warning: the meeting runs on the
            // ones that did, and the operator is told which it is missing.
            IReadOnlyList<DeviceSelection> missing = resolution.Missing;
            _dispatcher.Post(() =>
            {
                ShowMeetingRunning();
                ApplyStatus(tally.Status);
                if (missing.Count > 0)
                    _view.ShowNotice(
                        "Some devices unavailable",
                        $"Skipped: {string.Join(", ", missing.Select(DescribeSelection))}",
                        NoticeKind.Warning);
            });
        }
        catch (Exception ex) when (
            ex is HttpRequestException
                or OperationCanceledException
                or JsonException
                or InvalidOperationException
                or ExternalException
                or NotSupportedException
                or ArgumentException)
        {
            // Pre-flight or device-open failure: classify the cause and return the menu to
            // idle with a clear message. Includes the session-mint timeout
            // (OperationCanceledException) and a malformed new-session response (JsonException)
            // so neither can escape this fire-and-forget task and wedge the shell on
            // "Starting…". The filter keeps this off CodeQL's catch-of-all radar.
            StartFailure failure = StartFailure.Classify(ex, settings.Host, settings.Port);
            FailToIdle("Could not start meeting", failure.Message);
        }
        finally
        {
            // Released on every exit that did not get as far as handing it over: the non-Ok
            // early return, or a throw from the resolve or the mint. Null from the StartAll
            // call onwards, so a running meeting keeps its devices and a refused set has
            // already had them released by the orchestrator.
            enumerator?.Dispose();
        }
    }

    /// <summary>
    /// Open one resolved device behind the capture seam and add a pipeline for it.
    /// Best-effort: a device that fails to OPEN is surfaced and skipped, so a dead loopback
    /// does not stop the mic from recording. Opening is the runtime's own stage, which is why
    /// <see cref="CaptureOrchestrator"/> cannot own it; the orchestrator owns the symmetric
    /// START-failure half, capture.Start throwing inside TapSession.Begin. The filter names
    /// the enumerator seam's declared failures.
    /// </summary>
    private void TryAddSpec(
        List<PipelineSpec> into, IAudioDeviceEnumerator enumerator,
        ResolvedDevice resolved, TapConnectionOptions options)
    {
        try
        {
            into.Add(new PipelineSpec(
                enumerator.Open(resolved.Device), options, resolved.Gate.ToGateOptions()));
        }
        catch (Exception ex) when (
            ex is ExternalException or NotSupportedException or ArgumentException or InvalidOperationException)
        {
            _dispatcher.Post(() =>
                _view.ShowNotice($"Could not open {resolved.Device.Name}", ex.Message, NoticeKind.Warning));
        }
    }

    /// <summary>The operator-facing reason a device selection cannot start a meeting. Each
    /// verdict names the fix rather than the fault: the operator's next move is in Settings
    /// either way, and which tab differs.</summary>
    /// <summary>Name a selection the way the operator chose it, for the "skipped" notice: a
    /// follow-default entry has no device to name, so it is described by its role.</summary>
    private static string DescribeSelection(DeviceSelection selection) => selection switch
    {
        DeviceSelection.FollowDefault { Flow: DeviceFlow.Capture } => "default microphone",
        DeviceSelection.FollowDefault { Flow: DeviceFlow.Render } => "default system audio",
        DeviceSelection.Pinned pinned => string.IsNullOrEmpty(pinned.Name) ? pinned.DeviceId : pinned.Name,
        _ => selection.Identity,
    };

    private static string DescribeVerdict(SelectionVerdict verdict) => verdict switch
    {
        SelectionVerdict.NothingToCapture =>
            "None of your selected devices are available. Check the Devices tab in Settings.",
        SelectionVerdict.DuplicateIdentity =>
            "Two devices share an identity. Give each a distinct identity in Settings.",
        _ => "Cannot start with the current device selection.",
    };

    /// <summary>Roll the menu back to idle and surface why. Marshalled, because every caller
    /// reaches it from a continuation; <see cref="Failed"/> is the same three actions for a
    /// caller already on the UI thread.</summary>
    private void FailToIdle(string title, string message) =>
        _dispatcher.Post(() => Failed(new TrayStatus.Error(message), title, message));

    /// <summary>Surface a failure and return the menu to idle. The one spelling of "something
    /// went wrong and the operator can try again", so a start failure and a pipeline failure
    /// cannot drift into doing these three things in two different orders.</summary>
    private void Failed(TrayStatus status, string title, string message)
    {
        ShowIdleControls();
        ApplyStatus(status);
        _view.ShowNotice(title, message, NoticeKind.Warning);
    }

    /// <summary>Both commands off: a meeting is starting, ending, or a pipeline is in flight.
    /// Named rather than a bool pair at each call site, because "false, false" reads as an
    /// error state and is in fact the normal one three times over.</summary>
    private void ShowBusy() => _view.SetMenuState(canStart: false, canEnd: false);

    /// <summary>A meeting is streaming: End is the only move.</summary>
    private void ShowMeetingRunning() => _view.SetMenuState(canStart: false, canEnd: true);

    /// <summary>Nothing running: Start is the only move.</summary>
    private void ShowIdleControls() => _view.SetMenuState(canStart: true, canEnd: false);

    /// <summary>
    /// Hand the runtime the running event loop. Call this ONCE, from the shell, as soon as its
    /// loop is pumping and its <see cref="IDispatcher"/> can actually deliver: everything below
    /// marshals through the view, and a constructor runs too early for that on either platform
    /// (WinForms has not installed its SynchronizationContext yet; AppKit has not finished
    /// launching). Resumes a pipeline a previous session left running, which the Recorder kept
    /// going across both restarts. Polls only: no drain, no re-trigger.
    /// </summary>
    public void Startup()
    {
        MeetingState? state = _deps.StateStore.Load();
        if (state is null)
            return; // the common case: a fresh launch with no meeting to resume

        BridgeSettings settings;
        lock (_gate)
            settings = _settings;

        ShowBusy();
        ApplyStatus(new TrayStatus.Processing("Resuming…"));
        Task resume = RunPipelineFlowAsync(
            settings, state.SessionId,
            run: (controller, ct) => controller.ResumeAsync(ct),
            drainAsync: null); // nothing of ours is streaming: a previous process owned those taps
        lock (_gate)
            _endTask = resume;
    }

    /// <summary>The settings currently in force. The shell seeds its dialog from this rather
    /// than from its own copy, so an edit that failed to reach disk still governs the
    /// session.</summary>
    public BridgeSettings Settings
    {
        get { lock (_gate) return _settings; }
    }

    /// <summary>
    /// Apply an edited settings object: publish it, persist it, and push the per-device gate
    /// tuning to any running pipelines (issues #149, #153).
    ///
    /// Editing while a meeting is live is allowed. Connection and device changes bind at the
    /// next Start (those pipelines bound them at Begin), but the level-gate knobs re-tune the
    /// running pipelines in place, so a sensitivity change takes effect with no Stop/Start,
    /// touching only the devices whose tuning changed.
    /// </summary>
    public void ApplySettings(BridgeSettings updated)
    {
        ArgumentNullException.ThrowIfNull(updated);

        // Publish under the same lock every other reader takes (Start, End, Startup). Those
        // read it from thread-pool continuations, so "they all happen to run on the UI thread"
        // was never true, and an unlocked write of a reference field is not ordered against
        // them.
        lock (_gate)
            _settings = updated;

        try
        {
            _deps.SettingsStore.Save(updated);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // The settings directory could not be written (permissions, full disk, a file
            // standing where the directory should be). Keep the new settings for this session
            // and tell the operator they will not persist: throwing their edit away because
            // the disk is unavailable would be the worse failure.
            _view.ShowNotice("Settings not saved", ex.Message, NoticeKind.Warning);
        }

        // Grab the orchestrator under the lock (a Start may publish it from a thread-pool
        // continuation), then call out WITHOUT holding it: UpdateGates is a quick atomic
        // fan-out and should not run under _gate. The per-identity map routes each device's new
        // tuning to its own pipeline; one whose identity is not running is skipped. No meeting
        // means null means a no-op. Applied even if the save above failed, so the in-memory
        // re-tune still reaches the pipelines for this session.
        Meeting? running;
        lock (_gate)
            running = _meeting;
        running?.Orchestrator.UpdateGates(updated.ToGateOptionsByIdentity());
    }

    /// <summary>The in-flight End (or Resume) flow, so a test can await the real one.</summary>
    public Task? EndTask
    {
        get { lock (_gate) return _endTask; }
    }

    /// <summary>
    /// End meeting (issue #107): close every open tap, drain it, and then run the
    /// end-of-meeting pipeline, showing progress and the finished summary. Detaches the
    /// running meeting atomically so nothing else can race it; with nothing running there is
    /// nothing to end.
    /// </summary>
    public void End()
    {
        Meeting? meeting = TakeMeeting();
        if (meeting is null)
            return;

        BridgeSettings settings;
        lock (_gate)
            settings = _settings;

        // Busy guard: both commands disabled for the whole pipeline, so a second End cannot
        // fire a second one.
        ShowBusy();
        ApplyStatus(new TrayStatus.Ending());
        Task end = EndAsync(settings, meeting);
        lock (_gate)
            _endTask = end;
    }

    private Task EndAsync(BridgeSettings settings, Meeting meeting)
    {
        string sessionId = meeting.SessionId;
        bool process = settings.ProcessOnEnd;
        // Persist the resume state and the Past-meetings entry only when a pipeline will
        // actually run: a record-only meeting has no pipeline to resume across a restart and
        // no summary to re-open, so it stays out of both. Both writes are best-effort, and a
        // failed write never breaks the drain.
        if (process)
        {
            _deps.StateStore.Save(new MeetingState { SessionId = sessionId });
            _deps.HistoryStore.Append(new MeetingRecord { SessionId = sessionId, StartedAt = meeting.StartedAt });
        }
        return RunPipelineFlowAsync(
            settings, sessionId,
            // Record-only (ProcessOnEnd == false) still drains below but skips the
            // trigger/poll, ending at a terminal Saved view; the default runs the full
            // pipeline (issue #107).
            run: (controller, ct) => controller.EndAsync(triggerPipeline: process, cancellationToken: ct),
            // Close every open tap (gate close + Drain) BEFORE the pipeline strips; the
            // controller awaits this to completion before it triggers. End-meeting teardown is
            // ONE call, drain then stop+dispose then release the enumerator, so it cannot be
            // reduced to a drain that leaks the devices and lets them stream past the barrier,
            // and a teardown that fails part-way still releases what it can (see
            // EndMeetingAsync).
            drainAsync: () => meeting.Orchestrator.EndMeetingAsync());
    }

    // Build the controller, wire its emissions to the UI thread, run the flow (End or Resume),
    // and always clear the persisted state when it terminates. The one place the End and
    // Resume paths share: they differ only in the run delegate and the drain.
    private async Task RunPipelineFlowAsync(BridgeSettings settings, string sessionId,
        Func<MeetingController, CancellationToken, Task> run, Func<Task>? drainAsync)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        var controller = new MeetingController(
            control, sessionId,
            pollDelay: ct => Task.Delay(_budgets.PollInterval, ct), drainAsync: drainAsync);
        controller.Updated += view => _dispatcher.Post(() => RenderPipeline(view));
        controller.OperatorNotice += message =>
            _dispatcher.Post(() => _view.ShowNotice("Meeting", message, NoticeKind.Warning));

        // The flow's poll loop would otherwise run uncancellable: teardown ends the shell and
        // leaves it polling into a dead UI. Publish a source teardown can cancel. Never
        // disposed, deliberately: a CancellationTokenSource with no linked source, no timer and
        // no observed WaitHandle holds nothing unmanaged, so Dispose is an optimisation, while
        // Cancel-after-Dispose throws. Not disposing removes the race rather than arbitrating
        // it.
        var cancellation = new CancellationTokenSource();
        lock (_gate)
            _flowCancellation = cancellation;

        bool handled = false;
        try
        {
            await run(controller, cancellation.Token).ConfigureAwait(false);
            handled = true;
        }
        catch (Exception ex) when (
            ex is HttpRequestException or OperationCanceledException or InvalidOperationException)
        {
            // The Recorder is unreachable / timed out / refused the trigger after the taps
            // drained: classify it and surface a clear error so the shell does not wedge on a
            // processing state. The filter keeps this off CodeQL's catch-all radar.
            StartFailure failure = StartFailure.Classify(ex, settings.Host, settings.Port);
            _dispatcher.Post(() => FailPipeline(failure.Message));
            handled = true;
        }
        finally
        {
            _deps.StateStore.Clear();
            lock (_gate)
                if (ReferenceEquals(_flowCancellation, cancellation))
                    _flowCancellation = null; // this flow is over; teardown has nothing to cancel
            if (!handled)
                // An exception OUTSIDE the filter above is escaping this fire-and-forget task.
                // Nobody observes it, and both commands are disabled with the header stuck on
                // "Ending meeting…": the shell is unusable until it is restarted. The exception
                // still propagates (it is not this method's to classify); this only returns the
                // menu to a usable state on its way out.
                _dispatcher.Post(() => FailPipeline("The meeting could not be completed."));
        }
    }

    // Render a MeetingController emission: the status line tracks the pipeline phase, and the
    // terminal phases open the summary window / surface the failure.
    private void RenderPipeline(PipelineView view)
    {
        switch (view.Phase)
        {
            case PipelinePhase.Ending:
                ApplyStatus(new TrayStatus.Ending());
                break;
            case PipelinePhase.Running:
                ApplyStatus(new TrayStatus.Processing(view.Progress ?? "Processing…"));
                break;
            case PipelinePhase.Done:
                ApplyStatus(new TrayStatus.SummaryReady());
                _view.ShowNotice(
                    "Meeting summary ready", "Your meeting notes are ready.", NoticeKind.Information);
                _view.OpenMeetingWindow().Render(view); // opened straight at the finished summary (#107)
                ShowIdleControls();
                break;
            case PipelinePhase.Failed:
                FailPipeline(view.FailureReason ?? "The end-of-meeting pipeline failed.", view.FailureStage);
                break;
            case PipelinePhase.Saved:
                // Record-only End (ProcessOnEnd == false): the taps drained and the recordings
                // are saved on the Recorder, but nothing was transcribed or summarized. A brief
                // cue and straight back to idle: there is no summary window to open.
                _view.ShowNotice(
                    "Recording saved",
                    "The meeting was recorded. Transcribe or summarize it from the dashboard.",
                    NoticeKind.Information);
                ResetIdle();
                break;
            default:
                // Idle / Recording: a resumed session with no live pipeline; back to idle.
                ResetIdle();
                break;
        }
    }

    /// <summary>
    /// The local Past-meetings history (#168), newest first. Read from the store on every call
    /// rather than snapshotted, because the menu rebuilds its submenu from this each time it
    /// opens and has to reflect meetings ended since it was last shown. An unreadable history
    /// degrades to empty in the store, so this never throws and the menu shows its placeholder.
    /// </summary>
    public IReadOnlyList<MeetingRecord> PastMeetings() => _deps.HistoryStore.Load().Meetings;

    /// <summary>The most recently opened past meeting's poll, so a test can await the real one.
    /// Each open is independent (a window apiece), so this is the latest rather than all of
    /// them: nothing in the runtime waits on it.</summary>
    public Task? PastMeetingTask
    {
        get { lock (_gate) return _pastMeetingTask; }
    }

    private Task? _pastMeetingTask;

    /// <summary>
    /// Open a past meeting (#168) in a window of its OWN, isolated from the status line and the
    /// Start/End commands: re-opening last week's notes must never disturb a live meeting. The
    /// window shows its loading state immediately, then a <see cref="MeetingController"/> ride
    /// takes it to the summary (or to a "no longer available" failure). Read-only: it never
    /// drains and never re-triggers, so opening one alongside a live meeting, or alongside that
    /// meeting's own End, is safe.
    /// </summary>
    public void OpenPastMeeting(MeetingRecord record)
    {
        ArgumentNullException.ThrowIfNull(record);
        BridgeSettings settings;
        lock (_gate)
            settings = _settings;

        IMeetingWindow window = _view.OpenMeetingWindow();
        // Never disposed, deliberately, and this is the considered answer to CodeQL's
        // cs/local-not-disposed here, not an oversight.
        //
        // Two parties hold it: the window, which cancels on close, and the poll loop, which
        // reads its token until it returns. Their lifetimes end in EITHER order, so every
        // disposal point is wrong for one of them: dispose on close and the loop is left holding
        // a dead source; dispose when the loop ends and a later close calls Cancel on a disposed
        // one, which throws, out of a fire-and-forget task with nothing to report it. Disposal
        // can be made safe (unhook the close handler first, on the UI thread both run on), but
        // only by reinstating the arbitration protocol this replaced, and there is nothing on
        // the other side of that trade: a CancellationTokenSource with no linked source, no
        // timer and no observed WaitHandle holds nothing unmanaged, so Dispose is an
        // optimisation and the question does not arise. The behaviour it protects, that the loop
        // always holds a live token so closing the window stops it quietly rather than rendering
        // a failure, is pinned in MeetingViewDriverTests.
        var cancellation = new CancellationTokenSource();
        window.Closed += cancellation.Cancel;

        Task open = OpenPastMeetingAsync(settings, record.SessionId, window, cancellation.Token);
        lock (_gate)
            _pastMeetingTask = open;
    }

    private async Task OpenPastMeetingAsync(
        BridgeSettings settings, string sessionId, IMeetingWindow window,
        CancellationToken cancellationToken)
    {
        using var control = new ControlClient(
            settings.Host, settings.Port, settings.Tls, settings.Token,
            allowSelfSignedCert: settings.AllowSelfSignedCert);
        var controller = new MeetingController(
            control, sessionId, pollDelay: ct => Task.Delay(_budgets.PollInterval, ct));
        // The render-marshalling and the ride to the summary are MeetingViewDriver's; this
        // supplies the ControlClient, the dispatcher and the window.
        await MeetingViewDriver
            .DriveAsync(controller, window, _dispatcher, cancellationToken)
            .ConfigureAwait(false);
    }

    private void FailPipeline(string reason, string? stage = null) =>
        Failed(
            new TrayStatus.PipelineFailed(reason),
            "Meeting summary failed",
            stage is null ? reason : $"{stage}: {reason}");

    private void ResetIdle()
    {
        ShowIdleControls();
        ApplyStatus(new TrayStatus.Idle());
    }

    /// <summary>
    /// Tear everything down and then let the shell go. AWAITABLE rather than blocking, because
    /// on AppKit the caller IS the main run loop: the WinForms shell could block it twice (for
    /// a start still in flight, then for the pipelines to close) only because every await here
    /// uses ConfigureAwait(false), so no continuation needs that thread to progress. That is an
    /// unstated whole-file invariant, and it has no AppKit analogue: the problem there is
    /// occupying the main queue, not capturing a context.
    ///
    /// Ends with <see cref="ITrayView.Shutdown"/>, which is the shell's cue to release its UI
    /// and stop. Nothing is streaming and no callback is in flight by then.
    /// </summary>
    public async Task QuitAsync()
    {
        // Stop an in-flight End/Resume flow first: the shell's loop is about to go, so an
        // uncancelled poll loop would keep talking to the Recorder and posting into a dead
        // view for as long as the process lingered.
        CancellationTokenSource? flow;
        Task? start;
        lock (_gate)
        {
            flow = _flowCancellation;
            _flowCancellation = null;
            _quitting = true; // a start in flight must tear itself down, not publish
            start = _startTask;
        }
        flow?.Cancel();

        // Let a start that is mid-flight settle. Until it publishes, TakeMeeting below sees
        // nothing to take, so without this the captures would keep streaming, the meeting would
        // go undisposed and the detached session would stay open on the Recorder, all the way
        // until the process died. Bounded: the mint carries its own 20 s timeout, and quitting
        // must not hang behind a Recorder that accepted the connection and went quiet. Past the
        // bound the start is abandoned, which the _quitting flag above has already made safe.
        //
        // HOW it settled is not this method's business, and a start genuinely can fault AFTER
        // publishing, so the result is deliberately not inspected: whether there is a meeting
        // to tear down is read from the field below under _gate, never inferred from the task.
        // Guarded on IsCompleted, not just on null: _startTask stays non-null forever after the
        // first Start, so an unguarded WhenAny arms a fresh timer on every quit that follows a
        // finished meeting.
        if (start is { IsCompleted: false })
            await Task.WhenAny(start, Task.Delay(_budgets.StartSettleTimeout)).ConfigureAwait(false);

        Meeting? meeting = TakeMeeting();

        // Tear every pipeline down and release the enumerator behind them: the orchestrator
        // drains and closes them CONCURRENTLY, each bounded, and DisposeAsync is throw-free, so
        // this stays about one drain budget rather than N of them. The cap is a backstop; a
        // sub-second tail may drop on a hard quit.
        if (meeting is not null)
            await meeting.Orchestrator.DisposeAsync().AsTask()
                .WaitAsync(_budgets.QuitTeardownCap)
                .ConfigureAwait(false);

        // Marshalled like every other view touch. Both awaits above are ConfigureAwait(false),
        // so by here we are on a thread-pool thread: calling straight through would break the
        // contract ITrayView states, and neither shell can absorb it. AppKit's teardown
        // (NSStatusItem, NSApplication.Terminate) is main-thread-only, and the WinForms one has
        // to Dispose its NotifyIcon and end the message loop from the thread that owns them.
        _dispatcher.Post(_view.Shutdown);
    }

    /// <summary>
    /// A streaming meeting: the pipelines (which hold the enumerator their endpoints came out
    /// of), the detached session they tap into, and when it began. ONE value rather than three
    /// fields, because they are only ever published together and only ever detached together:
    /// split apart, every consumer had to re-derive "is a meeting running" from two of them,
    /// and clearing was three assignments that had to stay in step.
    /// </summary>
    private sealed record Meeting(
        CaptureOrchestrator Orchestrator,
        string SessionId,
        DateTimeOffset StartedAt);

    // Atomically detach the running meeting. Shared by End (which drains and triggers the
    // pipeline) and teardown (which tears down without touching the menu, since it is exiting),
    // so exactly one of the two ever gets it.
    private Meeting? TakeMeeting()
    {
        lock (_gate)
        {
            Meeting? meeting = _meeting;
            _meeting = null;
            return meeting;
        }
    }

    /// <summary>
    /// Apply a status to the view, skipping the one already showing: at the one point that
    /// knows what is on screen, rather than by each caller guessing. Every caller repeats
    /// itself: the per-device callbacks fire once per Utterance, and a running pipeline
    /// re-renders its progress line on every poll. <see cref="TrayStatus"/> is a record
    /// hierarchy, so value equality across all the call sites is free.
    /// </summary>
    private void ApplyStatus(TrayStatus status)
    {
        if (status == _lastStatus)
            return;
        _lastStatus = status;
        _view.ShowStatus(StatusView.For(status));
    }
}
