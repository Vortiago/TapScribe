using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// A scripted capture standing in for a WASAPI endpoint: it never produces audio (the tray
/// tests are about the shell's lifecycle, not the gate), but it records every lifecycle call
/// and can be told to fail at each of them the way a real endpoint does when it is
/// invalidated mid-meeting.
/// </summary>
internal sealed class FakeCapture(AudioFormat? format = null) : IAudioCapture
{
    private readonly TaskCompletionSource _disposeReached = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private EventHandler<AudioCapturedEventArgs>? _data;
    private int _disposals;

    public AudioFormat Format { get; } = format ?? new AudioFormat(16_000, 1, SampleKind.Int16);
    public bool Started { get; private set; }
    public bool Stopped { get; private set; }
    public bool Disposed => Volatile.Read(ref _disposals) > 0;

    /// <summary>How many times the owner disposed this — a leak fix must not turn into a
    /// double release.</summary>
    public int Disposals => Volatile.Read(ref _disposals);

    /// <summary>When set, unsubscribing from <see cref="DataAvailable"/> throws, which faults
    /// the End-meeting drain at its very first step (TapSession.DrainAllAsync detaches before
    /// it awaits). A contrived endpoint — the point is the SHELL's behaviour when its
    /// teardown call fails, and this is the only failure that reaches the drain rather than
    /// the dispose. It throws an IOException specifically: that is outside every catch filter
    /// on the End path, so it exercises the escape rather than the classified failure.</summary>
    public bool ThrowOnDetach { get; init; }

    /// <summary>When set, <see cref="Dispose"/> blocks until <see cref="ReleaseDispose"/> —
    /// a device that is slow to let go, which holds the End drain open so a test can act
    /// while the barrier is still running. Bounded, so it can never hang the run.</summary>
    public bool HoldDispose { get; init; }

    /// <summary>Completes once <see cref="Dispose"/> has been entered and is being held.</summary>
    public Task DisposeReached => _disposeReached.Task;

    private readonly TaskCompletionSource _release = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public void ReleaseDispose() => _release.TrySetResult();

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable
    {
        add => _data += value;
        remove
        {
            if (ThrowOnDetach)
                throw new IOException("endpoint invalidated");
            _data -= value;
        }
    }

    public bool IsMuted => false;
    public event EventHandler? MuteChanged { add { } remove { } }
    public event EventHandler<Exception?>? Failed { add { } remove { } }

    public void Start() => Started = true;

    public void Stop() => Stopped = true;

    /// <summary>The enumerator that handed this capture out, so a release can be recorded
    /// against the same teardown order the enumerator's own is.</summary>
    public FakeEnumerator? Owner { get; set; }

    public void Dispose()
    {
        Interlocked.Increment(ref _disposals);
        Owner?.RecordTeardown("capture");
        if (!HoldDispose)
            return;
        _disposeReached.TrySetResult();
        _release.Task.Wait(TimeSpan.FromSeconds(20)); // bounded: a stuck test fails, never hangs
    }
}

/// <summary>
/// A scripted device enumerator: hands out <see cref="FakeCapture"/>s for a fixed device
/// list, records whether the shell released it, and can fail an <see cref="Open"/> the way a
/// device that is busy or gone does. Disposable, like every real backend — the core seam
/// doesn't declare it, so this is also what proves the shell releases it through the seam.
/// </summary>
internal sealed class FakeEnumerator : IAudioDeviceEnumerator, IDisposable
{
    private readonly List<CaptureDevice> _devices = [];
    private readonly Dictionary<string, FakeCapture> _captures = new(StringComparer.Ordinal);
    private readonly List<string> _teardown = [];
    private int _opens;
    private int _disposals;

    public bool Disposed => Volatile.Read(ref _disposals) > 0;
    public int Disposals => Volatile.Read(ref _disposals);

    /// <summary>Captures handed out, oldest first — what the shell is on the hook for. Note
    /// "opened", not "started": the seam hands back a capture that is NOT running (a
    /// TapSession starts it), so this — never <see cref="FakeCapture.Started"/> — is what says
    /// the shell has taken ownership of something it now has to release.</summary>
    public IReadOnlyList<FakeCapture> Opened => _opened;

    private readonly List<FakeCapture> _opened = [];

    /// <summary>The order teardown actually happened in: one entry per capture released and
    /// one for the enumerator. Captures must come first — an enumerator handed its endpoint
    /// over to the capture it opened, so releasing it while a capture is still live inverts
    /// the ownership the Settings meter path spells out explicitly.</summary>
    public IReadOnlyList<string> TeardownOrder
    {
        get { lock (_teardown) return [.. _teardown]; }
    }

    internal void RecordTeardown(string what)
    {
        lock (_teardown)
            _teardown.Add(what);
    }

    public FakeCapture Add(string id, DeviceFlow flow, bool isDefault = true, FakeCapture? capture = null)
    {
        _devices.Add(new CaptureDevice(id, id, flow, isDefault));
        FakeCapture fake = capture ?? new FakeCapture();
        fake.Owner = this;
        _captures[id] = fake;
        return fake;
    }

    /// <summary>
    /// Make the Nth (1-based) <see cref="Open"/> throw, whichever device that turns out to be.
    /// Counted rather than named on purpose: a test about "the captures already opened when
    /// something threw" must not also depend on the order the shell happens to resolve devices
    /// in — name the failing device and a reordering silently turns the test into one that
    /// opens nothing at all and proves nothing.
    /// </summary>
    public int FailOpenNumber { get; set; }

    public IReadOnlyList<CaptureDevice> List() => [.. _devices];

    public IAudioCapture Open(CaptureDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);
        if (++_opens == FailOpenNumber)
            // An IOException is outside BOTH filters on this path — TryAddSpec's per-device
            // one (COM / NotSupported / Argument / InvalidOperation) and StartAsync's own — so
            // it escapes the build loop entirely instead of being skipped or classified. That
            // is what "any unexpected throw between the open and the handoff" means.
            throw new IOException($"opening '{device.Id}' failed unexpectedly");
        FakeCapture capture = _captures[device.Id];
        _opened.Add(capture);
        return capture;
    }

    public void Dispose()
    {
        Interlocked.Increment(ref _disposals);
        RecordTeardown("enumerator");
    }
}

/// <summary>In-memory stand-ins for the tray's two %APPDATA% files, so no test touches the
/// operator's real resume state or Past-meetings history. Also the observation point for
/// "the pipeline flow terminated" — the flow clears the resume state in its finally.</summary>
internal sealed class FakeStores : IMeetingStores
{
    private readonly object _lock = new();
    private MeetingState? _state;
    private MeetingHistory _history = MeetingHistory.Empty;
    private int _stateClears;

    private int _stateLoads;

    public MeetingState? LoadState()
    {
        lock (_lock)
        {
            _stateLoads++;
            return _state;
        }
    }

    /// <summary>How many times the shell has asked whether there is a meeting to resume.</summary>
    public int StateLoads
    {
        get { lock (_lock) return _stateLoads; }
    }

    public void SaveState(MeetingState state)
    {
        lock (_lock) _state = state;
    }

    public void ClearState()
    {
        lock (_lock)
        {
            _state = null;
            _stateClears++;
        }
    }

    /// <summary>How many times a pipeline flow has run to completion (its finally clears).</summary>
    public int StateClears
    {
        get { lock (_lock) return _stateClears; }
    }

    public MeetingHistory LoadHistory()
    {
        lock (_lock) return _history;
    }

    public void AppendHistory(MeetingRecord record)
    {
        lock (_lock) _history = _history.Append(record);
    }

    public void Seed(params MeetingRecord[] records)
    {
        lock (_lock)
            foreach (MeetingRecord record in records)
                _history = _history.Append(record);
    }
}

/// <summary>
/// The tray's notification-area presence, recorded instead of registered. This is the ONE
/// double that exists for the host rather than for the assertions: a real
/// <c>NotifyIcon</c> with <c>Visible = true</c> calls <c>Shell_NotifyIcon</c>, which on a
/// runner with no interactive shell to answer it blocks for seconds and then took the test
/// host down with it (PR #428's first run: the whole assembly aborted before a single result,
/// with no assertion and no stack). Nothing under test needs an icon in the notification
/// area; the status the icon WOULD show is asserted through the menu's header line, which is
/// a plain object.
/// </summary>
internal sealed class FakeIndicator : ITrayIndicator
{
    public ContextMenuStrip? AttachedMenu { get; private set; }
    public StatusView? LastStatus { get; private set; }
    public List<(string Title, string Message)> Balloons { get; } = [];
    public bool Disposed { get; private set; }

    public void Attach(ContextMenuStrip menu) => AttachedMenu = menu;

    public void Show(StatusView view) => LastStatus = view;

    public void Warn(string title, string message) => Balloons.Add((title, message));

    public void Inform(string title, string message) => Balloons.Add((title, message));

    public void Dispose() => Disposed = true;
}

/// <summary>
/// Builds the settings + <c>TrayDependencies</c> a tray test drives the shell with, and owns
/// the mint gate. Named for what it is: the shell's whole outside world, scripted.
/// </summary>
internal sealed class TrayHarness
{
    private readonly TaskCompletionSource<string> _mint = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _mintReached = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public FakeEnumerator Enumerator { get; } = new();
    public FakeStores Stores { get; } = new();
    public FakeIndicator Indicator { get; } = new();

    /// <summary>Completes once the shell has asked for a detached session and is waiting.</summary>
    public Task MintReached => _mintReached.Task;

    /// <summary>Whether the mint answers immediately (the default) or parks until
    /// <see cref="CompleteMint"/> — the seam that holds a Start in flight.</summary>
    public bool HoldMint { get; init; }

    public const string SessionId = "2026-08-10T09-00-00";

    public void CompleteMint(string sessionId = SessionId) => _mint.TrySetResult(sessionId);

    public BridgeSettings Settings { get; init; } = DefaultSettings();

    /// <summary>The settings every tray test starts from, in ONE spelling so a variant can
    /// change the one field it cares about instead of re-typing the rest. They point at a port
    /// nothing listens on: a loopback connect there is refused immediately, so any request the
    /// shell DOES make fails fast and deterministically — no server, no timeout, no wall-clock
    /// in any assertion.</summary>
    public static BridgeSettings DefaultSettings() => new()
    {
        Host = "127.0.0.1",
        Port = 9, // discard/unassigned: connection refused, instantly
        Identity = "alice",
        Name = "Alice",
        Devices = [],
    };

    /// <summary>The shell's outside world, built once. A fresh record per read would hand
    /// StaShell.Build and a test that reads this directly DIFFERENT instances over the same
    /// harness — the allocation is trivial, the footgun is not.</summary>
    public TrayDependencies Dependencies => _dependencies ??= BuildDependencies();

    private TrayDependencies? _dependencies;

    private TrayDependencies BuildDependencies() => new(
        () => Enumerator,
        async (_, cancellationToken) =>
        {
            _mintReached.TrySetResult();
            if (!HoldMint)
                return SessionId;
            return await _mint.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        },
        Stores,
        () => Indicator,
        // No message loop is ever pumped here, so the resume kick would never fire anyway —
        // and production schedules it with a WinForms timer, which registers a native timer
        // window on the calling thread. Leaving one behind on a thread that then exits is a
        // teardown crash waiting to happen, so the test shell schedules nothing at all.
        static _ => { });
}
