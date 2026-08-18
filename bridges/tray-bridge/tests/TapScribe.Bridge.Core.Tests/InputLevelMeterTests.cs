using System.Buffers.Binary;
using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The display-only sampler behind the Settings input-level meter: it rides an
/// <see cref="IAudioCapture"/>, resamples each chunk to the gate's 16 kHz mono int16
/// scale and exposes the current level (envelope) for the dialog to paint — without
/// touching the tap/gate pipeline. Driven here with the scripted <see cref="FakeAudioCapture"/>,
/// so no real device is needed and CI runs it on Linux.
/// </summary>
public class InputLevelMeterTests
{
    [Fact]
    public void Level_RisesToTheInputRms_WhenLoud()
    {
        var capture = new FakeAudioCapture(Fixtures.RecorderFormat);
        using var meter = new InputLevelMeter(capture);
        meter.Start();

        capture.Emit(Fixtures.Loud(20));

        // Fixtures.Loud is a DC 8000 -> RMS 8000/32768 ≈ 0.244, preserved through the
        // near-identity resample. Instant attack, so it's reached without waiting.
        Assert.InRange(meter.Level, 0.243, 0.245);
    }

    [Fact]
    public void Level_ReleasesGraduallyTowardZero_AfterTheSoundStops()
    {
        var capture = new FakeAudioCapture(Fixtures.RecorderFormat);
        using var meter = new InputLevelMeter(capture);
        meter.Start();
        capture.Emit(Fixtures.Loud(20));
        double loud = meter.Level;

        // A short silence eases the bar down — not instantly to zero (gradual release)…
        capture.Emit(Fixtures.Silence(3));
        Assert.InRange(meter.Level, 0.0001, loud - 0.0001);

        // …and a long silence brings it to the floor.
        capture.Emit(Fixtures.Silence(80));
        Assert.InRange(meter.Level, 0.0, 0.01);
    }

    [Fact]
    public void Level_StaysOnTheGateScale_AcrossDeviceFormats()
    {
        // A loopback endpoint is commonly 48 kHz / stereo / float. The meter must resample
        // to the gate's 16 kHz scale, or the threshold line would be meaningless for system
        // audio. A DC 0.5 in both channels downmixes to 0.5 -> RMS 0.5 on the wire scale.
        var capture = new FakeAudioCapture(new AudioFormat(48_000, 2, SampleKind.Float32));
        using var meter = new InputLevelMeter(capture);
        meter.Start();

        capture.Emit(StereoFloatDc(0.5f, 4000));

        Assert.InRange(meter.Level, 0.49, 0.51);
    }

    [Fact]
    public void Level_IsZero_BeforeAnyAudio()
    {
        using var meter = new InputLevelMeter(new FakeAudioCapture(Fixtures.RecorderFormat));
        Assert.Equal(0.0, meter.Level);
    }

    [Fact]
    public void Lifecycle_ForwardsStartAndDispose_ToTheCapture()
    {
        var capture = new FakeAudioCapture(Fixtures.RecorderFormat);

        // using-statement so Dispose runs on every exit — including a throw from
        // Start()/the assert — without a manual finally: satisfies both
        // cs/dispose-not-called-on-throw and cs/missed-using-statement.
        using (var meter = new InputLevelMeter(capture))
        {
            meter.Start();
            Assert.True(capture.Started);
        }

        Assert.True(capture.Stopped);  // Dispose stops and releases the underlying capture
        Assert.True(capture.Disposed);
    }

    [Fact]
    public void Dispose_StopsUpdatingTheLevel()
    {
        // The dialog disposes the meter on close; a frame delivered afterwards must not move
        // a now-unowned reading. This covers the post-dispose delivery (unsubscribe + the
        // _disposed guard); the concurrent in-flight-callback window is guarded by the same
        // volatile flag but isn't reproducible against the synchronous FakeAudioCapture.
        var capture = new FakeAudioCapture(Fixtures.RecorderFormat);
        var meter = new InputLevelMeter(capture);
        // using-statement over the existing local: disposes on block exit (and
        // on throw) while keeping `meter` in scope so we can read Level after
        // disposal — the post-dispose delivery this test exercises. Satisfies
        // both cs/dispose-not-called-on-throw and cs/missed-using-statement.
        using (meter)
        {
            meter.Start();
        }

        capture.Emit(Fixtures.Loud(20));

        Assert.Equal(0.0, meter.Level);
    }

    [Fact]
    public void Dispose_WhenTheEndpointWasInvalidated_StillReleasesTheCapture()
    {
        // Unplug the mic with the Settings dialog open. The seam lets Stop raise its declared
        // native failure for exactly that, and the RELEASE is the half that still matters:
        // skipping it strands an endpoint every time a device goes away, and the throw leaves
        // the dialog's close path. TapSession.DisposeAsync holds this line already; this is
        // the other owner of a capture, and it holds it for the same reason.
        //
        // Reachable in practice on macOS, whose backend propagates the invalidation; WASAPI
        // swallows it inside its own Stop, so a Windows host never exercises this at all.
        var capture = new FakeAudioCapture(Fixtures.RecorderFormat)
        {
            StopError = new ExternalException("the endpoint was invalidated", -66748),
        };
        var meter = new InputLevelMeter(capture);
        meter.Start();

        Assert.Null(Record.Exception(meter.Dispose));

        Assert.True(capture.Stopped);
        Assert.Equal(1, capture.Disposals);
    }

    [Theory]
    [InlineData(-0.1)]
    [InlineData(1.0)]
    [InlineData(1.5)]
    public void Constructor_RejectsAnOutOfRangeRelease(double release) =>
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            new InputLevelMeter(new FakeAudioCapture(Fixtures.RecorderFormat), release));

    private static byte[] StereoFloatDc(float amplitude, int frames)
    {
        var bytes = new byte[frames * 2 * sizeof(float)]; // 2 interleaved channels
        for (int i = 0; i < frames * 2; i++)
            BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(i * sizeof(float), sizeof(float)), amplitude);
        return bytes;
    }
}
