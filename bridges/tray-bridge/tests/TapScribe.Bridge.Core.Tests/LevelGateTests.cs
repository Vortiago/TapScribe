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

    [Fact]
    public void UpdateTuning_RetunesThreshold_ForSubsequentFrames()
    {
        // Start deaf: a loud frame (RMS 0.244) sits below the 0.3 open threshold.
        var gate = new LevelGate(Opts(threshold: 0.3));
        Assert.Empty(gate.Push(Frame(8000)));
        Assert.False(gate.IsOpen);

        // Retune sensitive at runtime — no teardown — and the same level now opens.
        gate.UpdateTuning(Opts(threshold: 0.05));

        GateEvent opened = Assert.Single(gate.Push(Frame(8000)));
        Assert.Equal(GateEventKind.Opened, opened.Kind);
        Assert.True(gate.IsOpen);
    }

    [Fact]
    public void UpdateTuning_KeepsAnOpenUtteranceIntact_AndAppliesTheNewHangover()
    {
        // Open an utterance, then two silent frames — under the old hangover (5 frames)
        // it stays open.
        var gate = new LevelGate(Opts(hangoverMs: 100));
        var opening = PushAll(gate, Frame(8000), Frame(1000), Frame(1000));
        Assert.Equal(GateEventKind.Opened, opening[0].Kind);
        Assert.True(gate.IsOpen);
        Assert.DoesNotContain(opening, e => e.Kind == GateEventKind.Closed);

        // Shorten the hangover mid-utterance: the open utterance is NOT torn down (still
        // open, no spurious boundary emitted at the update).
        gate.UpdateTuning(Opts(hangoverMs: 40)); // 2 silent frames now closes
        Assert.True(gate.IsOpen);

        // Under the OLD hangover this next silent frame (3rd in a row) wouldn't close;
        // under the NEW 2-frame hangover it does — and it's the SAME utterance, never
        // re-opened.
        var closing = PushAll(gate, Frame(1000));
        Assert.Equal(GateEventKind.Closed, closing[^1].Kind);
        Assert.False(gate.IsOpen);
        Assert.DoesNotContain(closing, e => e.Kind == GateEventKind.Opened);
    }

    [Fact]
    public void UpdateTuning_AppliesTheNewPreRollWindow()
    {
        // Pre-roll starts disabled, then is grown to 3 frames at runtime.
        var gate = new LevelGate(Opts(preRollMs: 0));
        gate.UpdateTuning(Opts(preRollMs: 60));

        byte[] q1 = Frame(1001), q2 = Frame(1002), q3 = Frame(1003), loud = Frame(8000);
        var events = PushAll(gate, q1, q2, q3, loud);

        // The newly-enabled pre-roll replays the three buffered quiet frames, oldest
        // first, before the triggering frame.
        Assert.Equal(q1, events[0].Frame);
        Assert.Equal(Bytes(q1, q2, q3, loud), StreamedFrames(events));
    }

    [Fact]
    public void UpdateTuning_RejectsInvalidOptions_LeavingCurrentTuningInPlace()
    {
        var gate = new LevelGate(Opts(threshold: 0.05)); // sensitive

        Assert.Throws<ArgumentOutOfRangeException>(() => gate.UpdateTuning(Opts(threshold: 1.0)));
        Assert.Throws<ArgumentOutOfRangeException>(
            () => gate.UpdateTuning(new GateOptions { Hangover = TimeSpan.FromMilliseconds(-1) }));

        // A rejected update is atomic — it never swaps in a partial tuning, so the gate
        // still opens on the original sensitive threshold (RMS 0.122 >= 0.05).
        GateEvent opened = Assert.Single(gate.Push(Frame(4000)));
        Assert.Equal(GateEventKind.Opened, opened.Kind);
    }

    [Fact]
    public async Task UpdateTuning_IsSafeConcurrentlyWithPush()
    {
        var gate = new LevelGate(Opts());
        using var cts = new CancellationTokenSource();

        // Hammer UpdateTuning from another thread while Push drives the gate on this one.
        // The atomic snapshot swap means no torn reads and no exception for any
        // interleaving; every emitted frame stays well-formed (640 bytes, or empty for a
        // Closed boundary).
        Task tuner = Task.Run(() =>
        {
            bool sensitive = true;
            while (!cts.IsCancellationRequested)
            {
                gate.UpdateTuning(Opts(threshold: sensitive ? 0.05 : 0.3, hangoverMs: 40));
                sensitive = !sensitive;
            }
        });

        byte[] loud = Frame(8000), quiet = Frame(1000);
        for (int i = 0; i < 5000; i++)
            foreach (GateEvent ev in gate.Push(i % 2 == 0 ? loud : quiet))
                Assert.Equal(
                    ev.Kind == GateEventKind.Closed ? 0 : TapWire.FrameBytes,
                    ev.Frame.Length);

        cts.Cancel();
        await tuner;
    }
}
