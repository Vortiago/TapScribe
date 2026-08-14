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
    /// <summary>How long the session mint may take before Start gives up. Without a bound
    /// HttpClient waits its 100 s default, which would wedge the shell on "Starting…" against
    /// a host that accepts the connection and never replies.</summary>
    private static readonly TimeSpan MintTimeout = TimeSpan.FromSeconds(20);

    private readonly ITrayView _view;
    private readonly IDispatcher _dispatcher;
    private readonly BridgeDependencies _deps;
    private readonly object _gate = new();

    // Read and written under _gate, always: Start snapshots them and carries the snapshot into
    // thread-pool continuations, so there is no thread any of these is private to.
    private BridgeSettings _settings;
    private CaptureOrchestrator? _orchestrator;
    private IAudioDeviceEnumerator? _enumerator; // outlives the captures it opened
    private string? _sessionId;                  // the detached session the running meeting taps into
    private Task? _startTask;
    private DateTimeOffset _startedAt;           // wall-clock start, for the Past-meetings history (#168)

    private TrayStatus? _lastStatus;             // what the view is currently showing

    public BridgeRuntime(
        ITrayView view, IDispatcher dispatcher, BridgeDependencies dependencies, BridgeSettings settings)
    {
        ArgumentNullException.ThrowIfNull(view);
        ArgumentNullException.ThrowIfNull(dispatcher);
        ArgumentNullException.ThrowIfNull(dependencies);
        ArgumentNullException.ThrowIfNull(settings);
        _view = view;
        _dispatcher = dispatcher;
        _deps = dependencies;
        _settings = settings;
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
            // Already running, or already starting (the mint is a round-trip long).
            if (_orchestrator is not null || _startTask is { IsCompleted: false })
                return;
            settings = _settings;
        }

        // Disable Start now so a second click can't race a second meeting; the rest is async
        // and reports back through the dispatcher.
        _view.SetMenuState(canStart: false, canEnd: false);
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
        // 1) Resolve the operator's selection against what is present RIGHT NOW (follow-default
        //    binds to the current default). The base identity goes in so the collision check
        //    runs on the identity each device will actually tap under: a blank Speaker ID
        //    streams under the base one, so two devices can be distinct here and the same
        //    speaker at the Recorder.
        IAudioDeviceEnumerator enumerator = _deps.OpenEnumerator();
        TapConnectionOptions baseOptions = settings.ToConnectionOptions();
        ResolveResult resolution = DeviceSelection.Resolve(
            settings.EffectiveDevices, enumerator.List(), baseOptions.Identity);

        // 2) Mint a detached session. This doubles as the connection pre-flight: an unreachable
        //    Recorder or a rejected token throws here, before any device is opened.
        string sessionId;
        using (var cts = new CancellationTokenSource(MintTimeout))
            sessionId = await _deps.MintDetachedSession(settings, cts.Token).ConfigureAwait(false);

        // 3) One tap per resolved device, each routing into the one session under its own
        //    identity/name and its OWN gate (#151). ToTapOptions preserves the Resolved order,
        //    so options[i] pairs with Resolved[i].
        IReadOnlyList<TapConnectionOptions> perDevice = resolution.ToTapOptions(sessionId, baseOptions);
        var specs = new List<PipelineSpec>();
        for (int i = 0; i < resolution.Resolved.Count; i++)
        {
            ResolvedDevice resolved = resolution.Resolved[i];
            specs.Add(new PipelineSpec(
                enumerator.Open(resolved.Device), perDevice[i], resolved.Gate.ToGateOptions()));
        }

        // Which devices are actually streaming, and what that means for the status line, is the
        // core's DeviceTally. Touched from the dispatcher only (both callbacks marshal first),
        // which is the tally's contract.
        var tally = new DeviceTally(specs.Count);
        CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
            specs,
            onConnected: id => _dispatcher.Post(() => ApplyStatus(tally.Connected(id).Status)),
            onFailed: (id, ex) => _dispatcher.Post(() =>
            {
                TallyReport report = tally.Dropped(id);
                ApplyStatus(report.Status);
                // Only on a transition: a tap that cannot reach the Recorder reports once per
                // Utterance, so a device that dropped ONCE would otherwise notify all meeting.
                if (report.Transition)
                    _view.ShowNotice($"{id} stopped", ex.Message, NoticeKind.Warning);
            }));
            // No shared gate arg: each spec already carries its own per-device gate.

        lock (_gate)
        {
            _orchestrator = orchestrator;
            _enumerator = enumerator;
            _sessionId = sessionId;
            _startedAt = DateTimeOffset.Now;
        }

        _dispatcher.Post(() =>
        {
            _view.SetMenuState(canStart: false, canEnd: true);
            ApplyStatus(tally.Status);
        });
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
