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

    public void CompleteMint(string sessionId = SessionId) => _mint.TrySetResult(sessionId);

    public MeetingStateStore StateStore => _stateStore ??= new MeetingStateStore(_directory);

    private MeetingStateStore? _stateStore;

    public MeetingHistoryStore HistoryStore => _historyStore ??= new MeetingHistoryStore(_directory);

    private MeetingHistoryStore? _historyStore;

    public BridgeSettingsStore SettingsStore =>
        _settingsStore ??= new BridgeSettingsStore(new FakeTapTokenStore(), _directory, "runtime-test.json");

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
        async (_, cancellationToken) =>
        {
            _mintReached.TrySetResult();
            if (!HoldMint)
                return SessionId;
            return await _mint.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        },
        SettingsStore,
        StateStore,
        HistoryStore);

    private BridgeDependencies? _dependencies;

    public BridgeRuntime Build() => new(View, Dispatcher, Dependencies, Settings);

    /// <summary>Await the in-flight Start. Asserts only that it SETTLED: whether it succeeded
    /// is the caller's subject, and several tests are about starts that do not.</summary>
    public static async Task StartSettledAsync(BridgeRuntime runtime)
    {
        ArgumentNullException.ThrowIfNull(runtime);
        Task? start = runtime.StartTask;
        Assert.NotNull(start);
        await start.WaitAsync(TimeSpan.FromSeconds(30)).ConfigureAwait(false);
    }

    public void Dispose()
    {
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
