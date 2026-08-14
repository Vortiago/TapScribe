namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The tray as the runtime sees it, recorded instead of rendered. Everything a
/// <c>TrayContext</c> or an <c>NSStatusItem</c> would put on screen lands here as plain
/// values, so a lifecycle assertion reads as what the operator would see rather than as which
/// method was called.
///
/// Locked because the runtime marshals through an <see cref="IDispatcher"/> the tests run
/// inline: a view touch therefore happens on whichever thread the continuation landed on, and
/// the assertions run on the test's.
/// </summary>
internal sealed class FakeTrayView : ITrayView
{
    private readonly object _lock = new();
    private readonly List<(string Title, string Message, NoticeKind Kind)> _notices = [];
    private StatusView? _status;
    private bool _canStart = true;
    private bool _canEnd;

    /// <summary>The status the tray is currently showing, or null before the first render.</summary>
    public StatusView? LastStatus
    {
        get { lock (_lock) return _status; }
    }

    /// <summary>Every notice raised, in order: the balloon/notification trail.</summary>
    public IReadOnlyList<(string Title, string Message, NoticeKind Kind)> Notices
    {
        get { lock (_lock) return [.. _notices]; }
    }

    public bool CanStart
    {
        get { lock (_lock) return _canStart; }
    }

    public bool CanEnd
    {
        get { lock (_lock) return _canEnd; }
    }

    public void ShowStatus(StatusView status)
    {
        lock (_lock) _status = status;
    }

    public void ShowNotice(string title, string message, NoticeKind kind)
    {
        lock (_lock) _notices.Add((title, message, kind));
    }

    public void SetMenuState(bool canStart, bool canEnd)
    {
        lock (_lock)
        {
            _canStart = canStart;
            _canEnd = canEnd;
        }
    }

    /// <summary>Every meeting window opened, in order. A window per call is the contract, so
    /// the count is how a test says "the summary opened once" or "re-opening a past meeting
    /// did not reuse the live one's window".</summary>
    public IReadOnlyList<FakeMeetingWindow> Windows
    {
        get { lock (_lock) return [.. _windows]; }
    }

    private readonly List<FakeMeetingWindow> _windows = [];

    public IMeetingWindow OpenMeetingWindow()
    {
        var window = new FakeMeetingWindow();
        lock (_lock) _windows.Add(window);
        return window;
    }
}

/// <summary>A meeting window that records what was rendered into it instead of drawing it,
/// and can be closed the way an operator would.</summary>
internal sealed class FakeMeetingWindow : IMeetingWindow
{
    private readonly object _lock = new();
    private readonly List<PipelineView?> _rendered = [];

    public event Action? Closed;

    public bool IsDisposed { get; private set; }

    /// <summary>Everything rendered, in order. A null entry is the pre-first-poll loading
    /// state, which is a render like any other.</summary>
    public IReadOnlyList<PipelineView?> Rendered
    {
        get { lock (_lock) return [.. _rendered]; }
    }

    /// <summary>The last thing rendered, or null if nothing was.</summary>
    public PipelineView? Last
    {
        get { lock (_lock) return _rendered.Count == 0 ? null : _rendered[^1]; }
    }

    public void Render(PipelineView? view)
    {
        lock (_lock) _rendered.Add(view);
    }

    /// <summary>Close the window the way the operator does: the runtime is expected to stop
    /// polling, and a render after this must not reach a disposed window.</summary>
    public void Close()
    {
        IsDisposed = true;
        Closed?.Invoke();
    }
}

/// <summary>
/// Runs posted work immediately, on the posting thread. The tray's own harness queues instead,
/// because WinForms objects are thread-affine and a real message loop would make the timing
/// non-deterministic: neither is true here: the view is a plain object and the runtime's
/// ordering guarantees are the subject, so running inline keeps a test's assertions on the
/// line after the call that caused them.
/// </summary>
internal sealed class InlineDispatcher : IDispatcher
{
    public void Post(Action action)
    {
        ArgumentNullException.ThrowIfNull(action);
        action();
    }
}

/// <summary>
/// The runtime's whole outside world, scripted: the devices present, the mint, the view, and
/// three real stores pointed at a temp directory. Named for what it is: a test constructs one
/// of these, varies the one thing it is about, and drives the real
/// <see cref="BridgeRuntime"/> through it.
/// </summary>
internal sealed class RuntimeHarness : IDisposable
{
    private readonly TaskCompletionSource<string> _mint = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _mintReached = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly string _directory =
        Path.Join(Path.GetTempPath(), "tapscribe-runtime-" + Guid.NewGuid().ToString("N"));

    public FakeAudioDeviceEnumerator Enumerator { get; } = new();
    public FakeTrayView View { get; } = new();
    public InlineDispatcher Dispatcher { get; } = new();

    public const string SessionId = "2026-08-14T09-00-00";

    /// <summary>Completes once the runtime has asked for a detached session and is waiting.</summary>
    public Task MintReached => _mintReached.Task;

    /// <summary>Whether the mint answers immediately (the default) or parks until
    /// <see cref="CompleteMint"/>: the seam that holds a Start in flight.</summary>
    public bool HoldMint { get; init; }

    /// <summary>When set, the mint throws it instead of answering. The mint doubles as the
    /// connection pre-flight, so this is how a test models an unreachable Recorder or a
    /// rejected token without standing one up.</summary>
    public Exception? MintError { get; init; }

    public void CompleteMint(string sessionId = SessionId) => _mint.TrySetResult(sessionId);

    /// <summary>
    /// A live Recorder to run against, instead of the refused port. Set it and the mint goes
    /// through a real <see cref="ControlClient"/>, so End drains real taps, triggers the real
    /// end-of-meeting pipeline and polls it to a real summary. The End path is the one that
    /// genuinely has to talk to a Recorder: faking the control client there would leave every
    /// step of it unexercised. Pair with <see cref="RecorderSettings"/>.
    /// </summary>
    public FakeRecorder? Recorder { get; init; }

    /// <summary>The settings that reach <see cref="Recorder"/>: its port and the token it was
    /// started with.</summary>
    public static BridgeSettings RecorderSettings(FakeRecorder recorder)
    {
        ArgumentNullException.ThrowIfNull(recorder);
        return new BridgeSettings
        {
            Host = "127.0.0.1",
            Port = recorder.Port,
            Identity = "alice",
            Name = "Alice",
            Token = "tok-abc",
            Devices = [],
        };
    }

    private readonly HttpClient _http = new();

    public MeetingStateStore StateStore => _stateStore ??= new MeetingStateStore(_directory);

    private MeetingStateStore? _stateStore;

    public MeetingHistoryStore HistoryStore => _historyStore ??= new MeetingHistoryStore(_directory);

    private MeetingHistoryStore? _historyStore;

    /// <summary>Where the settings store writes. Overridden by the test that needs the save to
    /// FAIL, which is a property of the destination rather than of the settings.</summary>
    public string? SettingsStoreDirectory { get; init; }

    public BridgeSettingsStore SettingsStore =>
        _settingsStore ??= new BridgeSettingsStore(
            new FakeTapTokenStore(), SettingsStoreDirectory ?? _directory, "runtime-test.json");

    private BridgeSettingsStore? _settingsStore;

    /// <summary>The settings every runtime test starts from, in ONE spelling so a variant can
    /// change the one field it cares about instead of re-typing the rest. They point at a port
    /// nothing listens on: a loopback connect there is refused immediately, so any request a
    /// pipeline DOES make fails fast and deterministically: no server, no timeout, and no
    /// wall-clock in any assertion.</summary>
    public BridgeSettings Settings { get; init; } = new()
    {
        Host = "127.0.0.1",
        Port = 9, // discard/unassigned: connection refused, instantly
        Identity = "alice",
        Name = "Alice",
        Devices = [],
    };

    /// <summary>Register a device the enumerator will report and hand out a capture for.</summary>
    public FakeAudioCapture AddDevice(string id, DeviceFlow flow, bool isDefault = true) =>
        Enumerator.Add(new CaptureDevice(id, id, flow, isDefault), Fixtures.RecorderFormat);

    public BridgeDependencies Dependencies => _dependencies ??= new BridgeDependencies(
        () => Enumerator,
        async (settings, cancellationToken) =>
        {
            _mintReached.TrySetResult();
            if (MintError is not null)
                throw MintError;
            if (Recorder is not null)
            {
                using var control = new ControlClient(
                    settings.Host, settings.Port, settings.Tls, settings.Token, _http);
                SessionIdInUse =
                    await control.CreateDetachedSessionAsync(cancellationToken).ConfigureAwait(false);
                return SessionIdInUse;
            }
            SessionIdInUse = SessionId;
            if (!HoldMint)
                return SessionId;
            return await _mint.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        },
        SettingsStore,
        StateStore,
        HistoryStore);

    private BridgeDependencies? _dependencies;

    /// <summary>The detached session the running meeting taps into, as the mint handed it out.
    /// Null until a Start reaches the mint.</summary>
    public string? SessionIdInUse { get; private set; }

    /// <summary>Budgets short enough that a flow settles inside a test's patience. The values
    /// are backstops in production, so shortening them changes no behaviour: what a test must
    /// never do is wait out the shipped 1.5 s poll interval per pipeline tick.</summary>
    public RuntimeBudgets Budgets { get; init; } = new()
    {
        PollInterval = TimeSpan.FromMilliseconds(10),
        StartSettleTimeout = TimeSpan.FromSeconds(5),
        QuitTeardownCap = TimeSpan.FromSeconds(5),
    };

    public BridgeRuntime Build() => new(View, Dispatcher, Dependencies, Settings, Budgets);

    /// <summary>Await the in-flight Start. Asserts only that it SETTLED: whether it succeeded
    /// is the caller's subject, and several tests are about starts that do not.</summary>
    public static async Task StartSettledAsync(BridgeRuntime runtime)
    {
        ArgumentNullException.ThrowIfNull(runtime);
        Task? start = runtime.StartTask;
        Assert.NotNull(start);
        await start.WaitAsync(TimeSpan.FromSeconds(30)).ConfigureAwait(false);
    }

    /// <summary>Await the in-flight End (or Resume) flow, on the same terms.</summary>
    public static async Task EndSettledAsync(BridgeRuntime runtime)
    {
        ArgumentNullException.ThrowIfNull(runtime);
        Task? end = runtime.EndTask;
        Assert.NotNull(end);
        await end.WaitAsync(TimeSpan.FromSeconds(30)).ConfigureAwait(false);
    }

    public void Dispose()
    {
        _http.Dispose();
        try
        {
            if (Directory.Exists(_directory))
                Directory.Delete(_directory, recursive: true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Teardown of a temp directory, running from a using-block where a test may
            // already be reporting its own failure: a file still held open by a store must not
            // throw over the top of the result the reader needs. What is lost is one temp
            // directory, which the OS reclaims.
        }
    }
}
