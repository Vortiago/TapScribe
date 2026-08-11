using System.Net.WebSockets;
using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// RED contract for issue #221 — the End-meeting drain barrier must await every tap's
/// FULL drain before the pipeline trigger fires.
///
/// The tray wires <see cref="MeetingController"/>'s <c>drainAsync</c> to
/// <see cref="CaptureOrchestrator.DisposeAsync"/>, which bounds each session's drain at
/// <c>TapSession.DisposeDrainTimeout = 2 s</c> — a bound designed for Quit ("so Stop/Quit
/// can't hang"). But a tap mid-drain across a blip has a real budget of
/// <c>TapStreamOptions.DrainBudget = 8 s</c>, so End can return after 2 s while a tap is
/// still flushing its buffered tail; the pipeline then strips/transcribes a WAV that is
/// still being appended to. The fix is a dedicated end-of-meeting teardown —
/// <see cref="CaptureOrchestrator.DrainAllAsync"/> — that awaits each session's drain to
/// completion (bounded only by each drain's own <c>DrainBudget</c>, not the 2 s Quit cap),
/// wired as the tray's End-meeting <c>drainAsync</c>. The 2 s bound stays on
/// <see cref="CaptureOrchestrator.DisposeAsync"/>/Quit.
///
/// These tests pin the CAUSAL happens-before, never a wall-clock duration (the bridge-E2E
/// timing-flake trap): a fake connection whose drain send is HELD on a
/// <see cref="TaskCompletionSource"/> the test releases, plus a long <c>DrainBudget</c> so
/// the ONLY thing that completes the drain is the release — so "DrainAllAsync is still
/// running" is a race-free <see cref="Task.IsCompleted"/> snapshot, not a timed wait.
/// </summary>
public class DrainBarrierTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);
    private static Task Immediate(CancellationToken _) => Task.CompletedTask;

    // A stream tuning whose DrainBudget is far longer than any test runs: the drain can
    // only be completed by the test's ReleaseDrain(), never by its own budget timing out,
    // so every assertion below is causal (a released TCS), not a duration.
    private static TapStreamOptions HeldDrainStream() => new()
    {
        Backoff = [TimeSpan.FromMilliseconds(10)],
        BackoffCap = TimeSpan.FromMilliseconds(20),
        BackoffJitter = 0,
        DrainBudget = TimeSpan.FromSeconds(30),
        PollInterval = TimeSpan.FromMilliseconds(10),
    };

    [Fact]
    public async Task DrainAllAsync_HoldsUntilTheTapFlushesItsTail_ThenCompletes()
    {
        var transport = new HeldDrainTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), HeldDrainStream(), transport.Create);

        capture.Emit(Loud(40));                       // opens an utterance; the pump connects and blocks on the send
        await transport.SendReached.WaitAsync(Wait);  // a send is now in flight and held

        // DrainAllAsync must AWAIT the tap's drain to completion — while the flush is held
        // it cannot have returned. A DisposeAsync-style 2 s cap (the bug) or a
        // return-immediately stub would let it complete here.
        Task drain = orchestrator.DrainAllAsync();
        Assert.False(drain.IsCompleted,
            "DrainAllAsync returned before the tap finished flushing its buffered tail");

        transport.ReleaseDrain();                     // the tail may flush now
        await drain.WaitAsync(Wait);                  // and only now does DrainAllAsync complete
        Assert.True(transport.SentCount > 0, "the buffered tail was never flushed to the connection");
    }

    [Fact]
    public async Task EndMeeting_TriggersThePipeline_OnlyAfterEndMeetingAsyncCompletes()
    {
        await using FakeRecorder rec = await FakeRecorder.StartAsync();
        using var http = new HttpClient();
        using var control = new ControlClient("127.0.0.1", rec.Port, tls: false, token: "tok-abc", http);
        const string session = "meet-drain-barrier";

        var transport = new HeldDrainTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), HeldDrainStream(), transport.Create);

        capture.Emit(Loud(40));
        await transport.SendReached.WaitAsync(Wait);

        // The tray's End-meeting barrier is EndMeetingAsync (drain-to-completion THEN
        // dispose), NOT the 2 s DisposeAsync (Quit) — wire the production path.
        var controller = new MeetingController(
            control, session, pollDelay: Immediate, drainAsync: () => orchestrator.EndMeetingAsync());
        Task ending = controller.EndAsync();

        // EndAsync awaits drainAsync() == EndMeetingAsync, which is held on the tap's
        // flush, so it CANNOT have reached TriggerPipelineAsync yet: the pipeline must
        // not fire while a WAV is still being appended.
        Assert.False(ending.IsCompleted, "End fired before the drain barrier completed");
        Assert.Equal(0, rec.TriggerCount(session));

        transport.ReleaseDrain();                     // the tail flushes, the barrier completes
        await ending.WaitAsync(Wait);

        Assert.Equal(1, rec.TriggerCount(session));   // the pipeline fired — after the drain, exactly once
        Assert.True(capture.Disposed, "End must dispose capture as part of the barrier, not leave it running");
    }

    [Fact]
    public async Task DrainAllAsync_DrainsEveryPipeline_AndCompletes_WhenTransportIsUp()
    {
        // Guardrail: with a live transport DrainAllAsync flushes and closes EVERY session's
        // tap and returns — so it can't pass the held cases above by being a no-op that
        // never drains, nor by draining only the first pipeline.
        var transport = new FakeTapTransport();
        var mic = new FakeAudioCapture(RecorderFormat);
        var system = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(mic, "mic"), Spec(system, "system")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        mic.Emit(Loud(40));
        system.Emit(Loud(40));
        await Poll.UntilAsync(
            () => transport.HasStreamed("mic") && transport.HasStreamed("system"),
            Wait, "both pipelines to stream");

        await orchestrator.DrainAllAsync().WaitAsync(Wait);

        Assert.Equal(2, transport.Connections.Count);
        Assert.All(transport.Connections, c => Assert.True(c.Closed, "a tap was left open after DrainAllAsync"));
    }

    [Fact]
    public async Task EndMeetingAsync_StopsAndDisposesCapture_SoNoAudioStreamsPastTheBarrier()
    {
        // Regression pin (the self-review deleted the End dispose, leaking the capture):
        // the End-meeting barrier must STOP + DISPOSE capture, so a speaker who keeps
        // talking after "End meeting" streams NO further frames — the pipeline can't
        // strip/transcribe audio captured after End. A drain-only End (no dispose)
        // leaves capture live and would fail this.
        var transport = new FakeTapTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), FastStream(), transport.Create);

        capture.Emit(Loud(40));
        await Poll.UntilAsync(() => transport.HasStreamed("mic"), Wait, "the pipeline to stream");

        await orchestrator.EndMeetingAsync().WaitAsync(Wait);

        Assert.True(capture.Stopped, "End must stop capture");
        Assert.True(capture.Disposed, "End must dispose capture (release the device)");

        int sentAtBarrier = transport.Connections.Sum(c => c.SentCount);
        capture.Emit(Loud(40));  // a speaker keeps talking after End meeting
        Assert.Equal(
            sentAtBarrier,
            transport.Connections.Sum(c => c.SentCount)); // capture is stopped → no post-barrier frames
    }

    [Fact]
    public async Task EndMeetingAsync_MintsNoNewUtterance_WhileTheTailDrainIsHeld()
    {
        // Regression pin for the drain-WINDOW leak (the sibling test above only
        // emits AFTER EndMeetingAsync returns, when capture is already disposed):
        // EndMeetingAsync awaits the un-capped tail drain BEFORE DisposeAsync
        // detaches capture. With capture events still attached during that
        // window, a gate close → re-open (silence past the hangover, then
        // speech) mints a NEW TapStream and streams post-End PCM into the
        // session — the harm the barrier exists to prevent. DrainAllAsync must
        // detach capture events up front, so speech during the held drain
        // opens nothing.
        var transport = new HeldDrainTransport();
        var capture = new FakeAudioCapture(RecorderFormat);
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [Spec(capture, "mic")],
            onConnected: _ => { }, onFailed: (_, _) => { },
            FastGate(), HeldDrainStream(), transport.Create);

        capture.Emit(Loud(40));                       // utterance #1; its send blocks on the hold
        await transport.SendReached.WaitAsync(Wait);
        int connectionsAtEnd = transport.ConnectionsCreated;

        Task ending = orchestrator.EndMeetingAsync(); // held on utterance #1's tail flush
        Assert.False(ending.IsCompleted, "End returned while the tail was still flushing");

        capture.Emit(Silence(4));                     // FastGate hangover (3 silent frames) → gate would close
        capture.Emit(Loud(4));                        // speech again → would re-open a still-attached gate
        Assert.Equal(connectionsAtEnd, transport.ConnectionsCreated); // no post-End utterance minted

        transport.ReleaseDrain();
        await ending.WaitAsync(Wait);
        Assert.Equal(connectionsAtEnd, transport.ConnectionsCreated); // still exactly the pre-End tap
    }
}

/// <summary>
/// A connection factory whose every <see cref="ITapConnection.SendFrameAsync"/> is HELD on
/// a <see cref="TaskCompletionSource"/> until <see cref="ReleaseDrain"/>, so a tap's drain
/// stays in flight with no wall-clock dependence — the drain completes only when the test
/// releases it. <see cref="SendReached"/> lets the test wait until a send is actually
/// blocked before probing whether the drain barrier is still running.
/// </summary>
internal sealed class HeldDrainTransport
{
    private readonly TaskCompletionSource _release = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _sendReached = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private int _sent;

    /// <summary>Completes once some connection's SendFrameAsync has been entered (and is now held).</summary>
    public Task SendReached => _sendReached.Task;

    /// <summary>Total frames that got past the hold (i.e. flushed after the release).</summary>
    public int SentCount => Volatile.Read(ref _sent);

    /// <summary>Unblock every held (and future) send, letting each tap flush its tail.</summary>
    public void ReleaseDrain() => _release.TrySetResult();

    /// <summary>Connections minted so far — one per <see cref="TapStream"/>. A SECOND one
    /// appearing during a held End drain means a post-End utterance was opened.</summary>
    public int ConnectionsCreated => Volatile.Read(ref _connections);
    private int _connections;

    public ITapConnection Create(TapConnectionOptions options)
    {
        Interlocked.Increment(ref _connections);
        return new Conn(this);
    }

    private sealed class Conn(HeldDrainTransport owner) : ITapConnection
    {
        public bool Closed { get; private set; }

        public Task ConnectAsync(CancellationToken cancellationToken = default) => Task.CompletedTask;

        public async Task SendFrameAsync(ReadOnlyMemory<byte> frame, CancellationToken cancellationToken = default)
        {
            owner._sendReached.TrySetResult();                          // a send is in flight
            await owner._release.Task.WaitAsync(cancellationToken).ConfigureAwait(false); // hold it until released
            Interlocked.Increment(ref owner._sent);
        }

        public Task CloseAsync(CancellationToken cancellationToken = default)
        {
            Closed = true;
            return Task.CompletedTask;
        }

        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }
}
