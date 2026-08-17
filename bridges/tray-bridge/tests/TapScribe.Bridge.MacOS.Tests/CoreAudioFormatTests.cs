using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// How a CoreAudio stream description becomes the <see cref="AudioFormat"/> the core
/// resampler consumes (#419). This is the Mac half of the classification
/// <c>WasapiCaptureBase</c> does for WASAPI, lifted OUT of the capture class: on Windows
/// the same logic is trapped inside a type that needs a real endpoint to construct, so it
/// is only ever exercised through one. Here it is a pure function over the fields
/// <c>AudioStreamBasicDescription</c> carries, and every layout a Mac can report is a row
/// in a table rather than a device somebody has to own.
/// </summary>
public class CoreAudioFormatTests
{
    [Fact]
    public void Classify_A48kHzStereoFloatStream_IsFloat32()
    {
        // The overwhelmingly common macOS input format: CoreAudio hands almost every
        // built-in and USB mic to a client as packed 32-bit float.
        var stream = new CoreAudioStreamFormat(
            SampleRate: 48_000,
            ChannelsPerFrame: 2,
            BitsPerChannel: 32,
            FormatId: CoreAudioFormatId.LinearPcm,
            FormatFlags: CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked);

        Assert.Equal(new AudioFormat(48_000, 2, SampleKind.Float32), CoreAudioFormat.Classify(stream));
    }

    [Fact]
    public void Classify_A16BitIntegerStream_IsInt16()
    {
        // The other layout the resampler reads. Rarer than float on macOS, but a device
        // that declares a 16-bit integer stream is exactly what the Windows sibling's
        // WaveFormatEncoding.Pcm arm accepts, and the seam is the same either side.
        var stream = new CoreAudioStreamFormat(
            SampleRate: 44_100,
            ChannelsPerFrame: 1,
            BitsPerChannel: 16,
            FormatId: CoreAudioFormatId.LinearPcm,
            FormatFlags: CoreAudioFormatFlags.IsSignedInteger | CoreAudioFormatFlags.IsPacked);

        Assert.Equal(new AudioFormat(44_100, 1, SampleKind.Int16), CoreAudioFormat.Classify(stream));
    }
}
