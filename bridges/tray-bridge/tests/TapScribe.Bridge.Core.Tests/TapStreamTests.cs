using System.Buffers.Binary;
using System.Diagnostics;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="TapStream"/>. One happy-path test runs against a real
/// in-process Kestrel /tap server to lock the wire integration with the real
/// <see cref="TapClient"/>. The resilience paths — reconnect with a stable
/// <c>utterance_id</c>, during-gap buffering with drop-oldest, and bounded Drain —
/// run against a <see cref="FakeTapTransport"/> whose connect/send failures are
/// deterministic, because a send-only client's blip timing against a real socket
/// is inherently unscriptable (the first send after a graceful drop succeeds into
/// the void before the reset is observed).
/// </summary>
public class TapStreamTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static TapStreamOptions FastOptions(int maxBufferBytes = 96_000, int drainMs = 4000) => new()
    {
        Backoff = [TimeSpan.FromMilliseconds(10), TimeSpan.FromMilliseconds(20)],
        BackoffCap = TimeSpan.FromMilliseconds(40),
        BackoffJitter = 0,
        MaxBufferBytes = maxBufferBytes,
        DrainBudget = TimeSpan.FromMilliseconds(drainMs),
        PollInterval = TimeSpan.FromMilliseconds(10),
    };

    // A 640-byte frame stamping its index in the first 4 bytes, so a recorder can
    // report exactly which frames arrived.
    private static byte[] IndexFrame(int index)
    {
        var bytes = new byte[TapWire.FrameBytes];
        BinaryPrimitives.WriteInt32LittleEndian(bytes, index);
        return bytes;
    }

    private static void EnqueueRange(TapStream stream, int from, int count)
    {
        for (int i = from; i < from + count; i++)
            stream.Enqueue(IndexFrame(i));
    }

    // --- happy path: real Kestrel /tap server, real TapClient ----------------

    [Fact]
    public async Task HappyPath_DeliversEveryFrame_AndClosesCleanly()
    {
        await using RecordingTapServer server = await RecordingTapServer.StartAsync();
        var options = new TapConnectionOptions
        {
            Host = "127.0.0.1",
            Port = server.Port,
            Identity = "mic",
            UtteranceId = "utt-1",
        };
        var stream = TapStream.Begin(options, FastOptions());

        EnqueueRange(stream, 0, 50);
        await stream.DrainAndDisposeAsync().WaitAsync(Wait);
        await Poll.UntilAsync(() => server.Connections.Count == 1 && server.Connections[0].ClosedNormally,
            Wait, "the server to see all frames and a clean close");

        RecordingTapServer.Conn conn = server.Connections[0];
        Assert.Equal("utt-1", conn.UtteranceId);
        Assert.Equal(Enumerable.Range(0, 50), conn.Indices.Order());
        Assert.True(conn.ClosedNormally);
        Assert.Equal(50, stream.FramesSent);
    }

    // --- resilience: deterministic fake transport ----------------------------

    [Fact]
    public async Task MidUtteranceBlip_ReconnectsWithSameUtteranceId_AndLosesNoFrame()
    {
        var transport = new FakeTapTransport();
        var options = new TapConnectionOptions { Identity = "mic", UtteranceId = "utt-blip" };
        var stream = TapStream.Begin(options, FastOptions(), connectionFactory: transport.Create);

        // First batch lands on connection 0.
        EnqueueRange(stream, 0, 20);
        await Poll.UntilAsync(() => transport.SentCount(0) >= 20, Wait, "conn0 to receive 20 frames");

        // Blip: the link drops and the Recorder is briefly unreachable, so the
        // client buffers the gap audio while it retries.
        transport.Up = false;
        EnqueueRange(stream, 20, 30); // gap frames, buffered during the outage
        await Poll.UntilAsync(() => transport.Connections.Count >= 2, Wait, "a reconnect attempt");

        transport.Up = true; // Recorder back
        await stream.DrainAndDisposeAsync().WaitAsync(Wait);

        Assert.True(transport.Connections.Count >= 2, "expected a reconnect");
        // Every connection reused the same, non-empty utterance_id => the Recorder
        // appends to one WAV instead of producing a second file.
        Assert.All(transport.Connections, c => Assert.Equal("utt-blip", c.UtteranceId));
        // No frame lost across the blip: the unsent frame stays buffered and the gap
        // frames are flushed on reconnect.
        var received = transport.Connections.SelectMany(c => c.Sent).ToHashSet();
        Assert.True(received.IsSupersetOf(Enumerable.Range(0, 50)), "every frame delivered across the blip");
        Assert.Equal(50, stream.FramesSent);
    }

    [Fact]
    public async Task MidUtteranceNativeFailure_IsTreatedAsTransport_AndTheUtteranceSurvives()
    {
        // A transport whose native stack reports failures as ExternalException rather than a
        // managed WebSocketException. That is a blip like any other, so the utterance must
        // reconnect under the same utterance_id; treating it as terminal kills the pump and
        // silently ends the tap mid-speech.
        var transport = new FakeTapTransport
        {
            Failure = static () => new ExternalException("the native transport stack failed"),
        };
        var options = new TapConnectionOptions { Identity = "mic", UtteranceId = "utt-native" };
        var stream = TapStream.Begin(options, FastOptions(), connectionFactory: transport.Create);

        EnqueueRange(stream, 0, 20);
        await Poll.UntilAsync(() => transport.SentCount(0) >= 20, Wait, "conn0 to receive 20 frames");

        transport.Up = false;
        EnqueueRange(stream, 20, 30); // gap frames, buffered while the native failures repeat
        await Poll.UntilAsync(() => transport.Connections.Count >= 2, Wait, "a reconnect attempt");

        transport.Up = true;
        await stream.DrainAndDisposeAsync().WaitAsync(Wait);

        Assert.All(transport.Connections, c => Assert.Equal("utt-native", c.UtteranceId));
        var received = transport.Connections.SelectMany(c => c.Sent).ToHashSet();
        Assert.True(received.IsSupersetOf(Enumerable.Range(0, 50)), "every frame delivered across the failure");
        Assert.Equal(50, stream.FramesSent);
    }

    [Fact]
    public async Task GapBuffer_DropsOldestFrames_PastTheCap()
    {
        var transport = new FakeTapTransport();
        var options = new TapConnectionOptions { UtteranceId = "utt-overflow" };
        // Cap the gap buffer at ~10 frames.
        var stream = TapStream.Begin(options, FastOptions(maxBufferBytes: 10 * TapWire.FrameBytes),
            connectionFactory: transport.Create);

        // Connect once so a later drop is treated as a recoverable blip.
        stream.Enqueue(IndexFrame(0));
        await Poll.UntilAsync(() => transport.SentCount(0) >= 1, Wait, "conn0 to receive frame 0");

        // Go down and stay down, then capture a long burst while offline.
        transport.Up = false;
        EnqueueRange(stream, 1, 200);
        await Poll.UntilAsync(() => stream.DroppedFrames > 0, Wait, "the gap buffer to overflow");

        transport.Up = true; // Recorder returns: only the newest frames survived
        await stream.DrainAndDisposeAsync().WaitAsync(Wait);

        var received = transport.Connections.SelectMany(c => c.Sent).ToHashSet();
        Assert.Contains(200, received);     // newest gap frame kept
        Assert.DoesNotContain(1, received); // oldest gap frame dropped
        Assert.True(received.Count <= 1 + 20, $"delivery should be bounded near the cap, got {received.Count}");
    }

    [Fact]
    public async Task Drain_FlushesBufferedTail_ThroughABlip_WithinBudget_AndClosesCleanly()
    {
        var transport = new FakeTapTransport();
        var options = new TapConnectionOptions { UtteranceId = "utt-drain" };
        var stream = TapStream.Begin(options, FastOptions(drainMs: 4000), connectionFactory: transport.Create);

        EnqueueRange(stream, 0, 10);
        await Poll.UntilAsync(() => transport.SentCount(0) >= 10, Wait, "conn0 to receive 10 frames");

        // The utterance ends (drain) while disconnected: the tail must still land.
        transport.Up = false;
        EnqueueRange(stream, 10, 10);
        await Poll.UntilAsync(() => transport.Connections.Count >= 2, Wait, "a reconnect attempt");
        transport.Up = true;

        var sw = Stopwatch.StartNew();
        await stream.DrainAndDisposeAsync().WaitAsync(Wait);
        sw.Stop();

        var received = transport.Connections.SelectMany(c => c.Sent).ToHashSet();
        Assert.True(received.IsSupersetOf(Enumerable.Range(0, 20)), "the drained tail should be delivered across the blip");
        Assert.True(transport.Connections[^1].Closed, "drain should close the final connection cleanly");
        Assert.True(sw.Elapsed < TimeSpan.FromSeconds(4), $"drain took too long: {sw.Elapsed}");
    }

    [Fact]
    public async Task Drain_GivesUp_WhenRecorderStaysDown_Bounded()
    {
        var transport = new FakeTapTransport();
        var options = new TapConnectionOptions { UtteranceId = "utt-giveup" };
        var stream = TapStream.Begin(options, FastOptions(drainMs: 300), connectionFactory: transport.Create);

        EnqueueRange(stream, 0, 5);
        await Poll.UntilAsync(() => transport.SentCount(0) >= 5, Wait, "conn0 to receive 5 frames");

        // Recorder vanishes and never comes back.
        transport.Up = false;
        EnqueueRange(stream, 5, 5);

        var sw = Stopwatch.StartNew();
        await stream.DrainAndDisposeAsync().WaitAsync(Wait); // must return, not hang
        sw.Stop();

        Assert.True(sw.Elapsed < TimeSpan.FromSeconds(3), $"drain should give up bounded, took {sw.Elapsed}");
    }

    [Fact]
    public async Task FirstConnectFailure_IsTerminal_AndDoesNotRetryForever()
    {
        var transport = new FakeTapTransport { Up = false }; // down from the start
        var options = new TapConnectionOptions { UtteranceId = "utt-bad" };

        var failed = new TaskCompletionSource<Exception>(TaskCreationOptions.RunContinuationsAsynchronously);
        var stream = TapStream.Begin(options, FastOptions(),
            onTerminalFailure: ex => failed.TrySetResult(ex), connectionFactory: transport.Create);

        Exception error = await failed.Task.WaitAsync(Wait);
        Assert.IsType<WebSocketException>(error);
        await stream.Completion.WaitAsync(Wait); // pump stopped, not retrying
        Assert.Equal(0, stream.FramesSent);
        Assert.All(transport.Connections, c => Assert.Empty(c.Sent));

        await stream.DisposeAsync();
    }
}
