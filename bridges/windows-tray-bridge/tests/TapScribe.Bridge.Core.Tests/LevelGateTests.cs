using System.Buffers.Binary;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Unit tests for the <see cref="LevelGate"/> — the Bridge-side Mute — driven by
/// synthetic 16 kHz mono int16 PCM. Frames are DC (every sample the same value)
/// so a frame's RMS is exactly <c>|value| / 32768</c>, which makes "above /
/// below threshold" deterministic without any real audio. The gate emits one
/// 640-byte frame per event: an <c>Opened</c> for the utterance's first frame,
/// then an <c>Audio</c> per following frame, then a <c>Closed</c>.
/// </summary>
public class LevelGateTests
{
    // One 640-byte / 20 ms frame whose every sample is <paramref name="value"/>.
    // RMS == |value| / 32768.
    private static byte[] Frame(short value)
    {
        var bytes = new byte[TapWire.FrameBytes];
        for (int i = 0; i < TapWire.FrameSamples; i++)
            BinaryPrimitives.WriteInt16LittleEndian(bytes.AsSpan(i * 2, 2), value);
        return bytes;
    }

    private static List<GateEvent> PushAll(LevelGate gate, params byte[][] frames)
    {
        var events = new List<GateEvent>();
        foreach (byte[] f in frames)
            events.AddRange(gate.Push(f));
        return events;
    }

    private static byte[] Bytes(params byte[][] parts) => parts.SelectMany(p => p).ToArray();

    // The PCM streamed for the utterance, in order, across Opened + Audio events.
    private static byte[] StreamedFrames(IEnumerable<GateEvent> events) =>
        events.Where(e => e.Kind != GateEventKind.Closed).SelectMany(e => e.Frame).ToArray();

    // value 8000 -> RMS 0.244 (loud); 1000 -> 0.0305 (quiet); threshold 0.1.
    private static GateOptions Opts(double threshold = 0.1, int hangoverMs = 100, int preRollMs = 0) =>
        new()
        {
            OpenThreshold = threshold,
            Hangover = TimeSpan.FromMilliseconds(hangoverMs),
            PreRoll = TimeSpan.FromMilliseconds(preRollMs),
        };

    [Fact]
    public void Silence_NeverOpens()
    {
        var gate = new LevelGate(Opts());
        var events = PushAll(gate, Enumerable.Range(0, 50).Select(_ => Frame(1000)).ToArray());

        Assert.Empty(events);
        Assert.False(gate.IsOpen);
    }

    [Fact]
    public void ThresholdCrossing_Opens_WithPreRollReplayed()
    {
        var gate = new LevelGate(Opts(preRollMs: 60)); // 3 frames of pre-roll
        byte[] q1 = Frame(1001), q2 = Frame(1002), q3 = Frame(1003), loud = Frame(8000);

        var events = PushAll(gate, q1, q2, q3, loud);

        Assert.Equal(GateEventKind.Opened, events[0].Kind);
        Assert.Equal(q1, events[0].Frame); // the oldest pre-roll frame opens the utterance
        Assert.All(events.Skip(1), e => Assert.Equal(GateEventKind.Audio, e.Kind));
        // The three quiet pre-roll frames, oldest first, then the triggering frame.
        Assert.Equal(Bytes(q1, q2, q3, loud), StreamedFrames(events));
        Assert.True(gate.IsOpen);
    }

    [Fact]
    public void PreRoll_KeepsOnlyTheMostRecentFrames()
    {
        var gate = new LevelGate(Opts(preRollMs: 60)); // capacity 3 frames
        byte[][] quiet = Enumerable.Range(1, 6).Select(i => Frame((short)(1000 + i))).ToArray();
        byte[] loud = Frame(8000);

        var events = PushAll(gate, [.. quiet, loud]);

        // Oldest three quiet frames dropped; only the last three survive as pre-roll.
        Assert.Equal(quiet[3], events[0].Frame);
        Assert.Equal(Bytes(quiet[3], quiet[4], quiet[5], loud), StreamedFrames(events));
    }

    [Fact]
    public void PreRollZero_OpensWithJustTheTriggeringFrame()
    {
        var gate = new LevelGate(Opts(preRollMs: 0));
        byte[] loud = Frame(8000);

        var events = PushAll(gate, Frame(1000), Frame(1000), loud);

        GateEvent opened = Assert.Single(events);
        Assert.Equal(GateEventKind.Opened, opened.Kind);
        Assert.Equal(loud, opened.Frame);
    }

    [Fact]
    public void StaysOpen_ThroughSilenceShorterThanHangover()
    {
        var gate = new LevelGate(Opts(hangoverMs: 100)); // close needs 5 silent frames
        // open, 4 quiet (< 5), then loud again.
        var events = PushAll(gate, Frame(8000), Frame(1000), Frame(1000), Frame(1000), Frame(1000), Frame(8000));

        Assert.DoesNotContain(events, e => e.Kind == GateEventKind.Closed);
        Assert.True(gate.IsOpen);
        Assert.Equal(GateEventKind.Opened, events[0].Kind);
        Assert.Equal(5, events.Count(e => e.Kind == GateEventKind.Audio)); // 4 quiet + 1 loud after the open
    }

    [Fact]
    public void Closes_AfterHangoverOfSilence()
    {
        var gate = new LevelGate(Opts(hangoverMs: 100)); // 5 silent frames
        var events = PushAll(gate, Frame(8000), Frame(1000), Frame(1000), Frame(1000), Frame(1000), Frame(1000));

        Assert.Equal(GateEventKind.Opened, events[0].Kind);
        Assert.Equal(5, events.Count(e => e.Kind == GateEventKind.Audio)); // the 5 trailing silent frames
        Assert.Equal(GateEventKind.Closed, events[^1].Kind);
        Assert.False(gate.IsOpen);
    }

    [Fact]
    public void MultipleUtterances_OpenAndCloseIndependently()
    {
        var gate = new LevelGate(Opts(hangoverMs: 40)); // 2 silent frames close it
        var events = PushAll(
            gate,
            Frame(8000), Frame(1000), Frame(1000),   // utterance 1, then close
            Frame(8000), Frame(1000), Frame(1000));  // utterance 2, then close

        Assert.Equal(2, events.Count(e => e.Kind == GateEventKind.Opened));
        Assert.Equal(2, events.Count(e => e.Kind == GateEventKind.Closed));
        Assert.False(gate.IsOpen);
    }

    [Fact]
    public void Threshold_GovernsWhetherAFrameIsActive()
    {
        // value 4000 -> RMS 0.122. High threshold treats it as silence; low opens.
        Assert.Empty(new LevelGate(Opts(threshold: 0.2)).Push(Frame(4000)));

        var low = new LevelGate(Opts(threshold: 0.05));
        GateEvent opened = Assert.Single(low.Push(Frame(4000)));
        Assert.Equal(GateEventKind.Opened, opened.Kind);
        Assert.True(low.IsOpen);
    }

    [Fact]
    public void SubFrameBytes_AreBufferedAcrossPushes()
    {
        var gate = new LevelGate(Opts(preRollMs: 0));
        byte[] loud = Frame(8000);

        // First 200 bytes of a frame: not enough to decide anything yet.
        Assert.Empty(gate.Push(loud.AsSpan(0, 200)));
        Assert.False(gate.IsOpen);

        // The rest completes the 640-byte frame and opens the utterance.
        GateEvent opened = Assert.Single(gate.Push(loud.AsSpan(200)));
        Assert.Equal(GateEventKind.Opened, opened.Kind);
        Assert.Equal(loud, opened.Frame);
    }

    [Fact]
    public void InvalidOptions_Throw()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new LevelGate(Opts(threshold: 1.0)));
        Assert.Throws<ArgumentOutOfRangeException>(() => new LevelGate(Opts(threshold: -0.1)));
        Assert.Throws<ArgumentOutOfRangeException>(() => new LevelGate(new GateOptions { Hangover = TimeSpan.FromMilliseconds(-1) }));
        Assert.Throws<ArgumentOutOfRangeException>(() => new LevelGate(new GateOptions { PreRoll = TimeSpan.FromMilliseconds(-1) }));
    }
}
