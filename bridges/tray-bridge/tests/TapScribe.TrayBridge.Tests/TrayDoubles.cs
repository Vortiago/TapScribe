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

    public void Dispose()
    {
        Interlocked.Increment(ref _disposals);
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
    private readonly HashSet<string> _failOpen = new(StringComparer.Ordinal);
    private int _disposals;

    public bool Disposed => Volatile.Read(ref _disposals) > 0;
    public int Disposals => Volatile.Read(ref _disposals);

    /// <summary>Captures handed out, oldest first — what the shell is on the hook for.</summary>
    public IReadOnlyList<FakeCapture> Opened { get; } = new List<FakeCapture>();

    public FakeCapture Add(string id, DeviceFlow flow, bool isDefault = true, FakeCapture? capture = null)
    {
        _devices.Add(new CaptureDevice(id, id, flow, isDefault));
        FakeCapture fake = capture ?? new FakeCapture();
        _captures[id] = fake;
        return fake;
    }

    /// <summary>Make <see cref="Open"/> throw for this device. <paramref name="unexpected"/>
    /// picks an exception OUTSIDE the shell's per-device catch filter, which is what turns a
    /// skippable device failure into an escape that strands its siblings.</summary>
    public void FailOpen(string id, bool unexpected = false)
    {
        _failOpen.Add(id);
        if (unexpected)
            UnexpectedFailures.Add(id);
    }

    public List<string> UnexpectedFailures { get; } = [];

    public IReadOnlyList<CaptureDevice> List() => [.. _devices];

    public IAudioCapture Open(CaptureDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);
        if (_failOpen.Contains(device.Id))
            throw UnexpectedFailures.Contains(device.Id)
                // Outside TrayContext's TryAddSpec filter (COM / NotSupported / Argument /
                // InvalidOperation), so it escapes the whole build loop.
                ? new IOException($"'{device.Id}' blew up unexpectedly")
                : new InvalidOperationException($"'{device.Id}' is in use");
        FakeCapture capture = _captures[device.Id];
        ((List<FakeCapture>)Opened).Add(capture);
        return capture;
    }

    public void Dispose() => Interlocked.Increment(ref _disposals);
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

    public MeetingState? LoadState()
    {
        lock (_lock) return _state;
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
/// Builds the settings + <c>TrayDependencies</c> a tray test drives the shell with, and owns
/// the mint gate. Named for what it is: the shell's whole outside world, scripted.
/// </summary>
internal sealed class TrayHarness
{
    private readonly TaskCompletionSource<string> _mint = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _mintReached = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public FakeEnumerator Enumerator { get; } = new();
    public FakeStores Stores { get; } = new();

    /// <summary>Completes once the shell has asked for a detached session and is waiting.</summary>
    public Task MintReached => _mintReached.Task;

    /// <summary>Whether the mint answers immediately (the default) or parks until
    /// <see cref="CompleteMint"/> — the seam that holds a Start in flight.</summary>
    public bool HoldMint { get; init; }

    public const string SessionId = "2026-08-10T09-00-00";

    public void CompleteMint(string sessionId = SessionId) => _mint.TrySetResult(sessionId);

    /// <summary>Connection settings pointing at a port nothing listens on: a loopback
    /// connect there is refused immediately, so any request the shell DOES make fails fast
    /// and deterministically — no server, no timeout, no wall-clock in any assertion.</summary>
    public BridgeSettings Settings { get; init; } = new()
    {
        Host = "127.0.0.1",
        Port = 9, // discard/unassigned: connection refused, instantly
        Identity = "alice",
        Name = "Alice",
        Devices = [],
    };

    public TrayDependencies Dependencies => new(
        () => Enumerator,
        async (_, cancellationToken) =>
        {
            _mintReached.TrySetResult();
            if (!HoldMint)
                return SessionId;
            return await _mint.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        },
        Stores);
}
