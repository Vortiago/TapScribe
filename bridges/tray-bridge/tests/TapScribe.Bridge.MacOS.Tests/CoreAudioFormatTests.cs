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

    // Every layout a Mac can report that the resampler cannot read. The Windows sibling
    // refuses on encoding and bit depth alone, which is all WASAPI shared mode can vary;
    // CoreAudio hands out the device's OWN description, so endianness and interleaving are
    // real answers here rather than constants, and each is its own row.
    public static TheoryData<string, CoreAudioStreamFormat> UnreadableLayouts() => new()
    {
        {
            "compressed rather than linear PCM",
            new CoreAudioStreamFormat(48_000, 2, 32, FourCharCode("aac "), CoreAudioFormatFlags.None)
        },
        {
            "24-bit integer, which is not two bytes per sample",
            new CoreAudioStreamFormat(
                48_000, 2, 24, CoreAudioFormatId.LinearPcm,
                CoreAudioFormatFlags.IsSignedInteger | CoreAudioFormatFlags.IsPacked)
        },
        {
            "64-bit float, which is not four bytes per sample",
            new CoreAudioStreamFormat(
                48_000, 2, 64, CoreAudioFormatId.LinearPcm,
                CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked)
        },
        {
            "big-endian samples, which the resampler reads backwards",
            new CoreAudioStreamFormat(
                48_000, 2, 32, CoreAudioFormatId.LinearPcm,
                CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked | CoreAudioFormatFlags.IsBigEndian)
        },
        {
            "channels in separate buffers, so no interleaved frame exists to hand on",
            new CoreAudioStreamFormat(
                48_000, 2, 32, CoreAudioFormatId.LinearPcm,
                CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked | CoreAudioFormatFlags.IsNonInterleaved)
        },
        {
            "no sample rate, which a device between formats reports",
            new CoreAudioStreamFormat(
                0, 2, 32, CoreAudioFormatId.LinearPcm,
                CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked)
        },
        {
            "no channels, which is nothing to capture",
            new CoreAudioStreamFormat(
                48_000, 0, 32, CoreAudioFormatId.LinearPcm,
                CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked)
        },
        {
            "samples padded inside a wider word, so the stride is not the sample size",
            new CoreAudioStreamFormat(
                48_000, 2, 16, CoreAudioFormatId.LinearPcm, CoreAudioFormatFlags.IsSignedInteger)
        },
    };

    [Theory]
    [MemberData(nameof(UnreadableLayouts))]
    public void Classify_ALayoutTheResamplerCannotRead_ThrowsNotSupported(
        string why, CoreAudioStreamFormat stream)
    {
        // NotSupportedException, not the seam's native-failure type: the device ANSWERED,
        // and the answer was unusable. IAudioDeviceEnumerator.Open declares the two
        // separately for exactly this reason, and a caller skipping a dead endpoint filters
        // on the other one.
        var thrown = Assert.Throws<NotSupportedException>(() => CoreAudioFormat.Classify(stream));

        // The operator sees this message when a device is skipped, so it has to name the
        // layout rather than say "unsupported format" and leave them guessing.
        Assert.Contains($"{stream.BitsPerChannel}-bit", thrown.Message, StringComparison.Ordinal);
        Assert.False(string.IsNullOrWhiteSpace(why));
    }

    [Fact]
    public void Classify_AMonoStreamFlaggedNonInterleaved_IsAccepted()
    {
        // Where the interleaving line sits, pinned rather than left to whoever reads the
        // refusal above. One channel has nothing to interleave WITH, so the bytes are
        // identical either way and refusing would strand a real mic over a flag that
        // describes nothing.
        var stream = new CoreAudioStreamFormat(
            SampleRate: 48_000,
            ChannelsPerFrame: 1,
            BitsPerChannel: 32,
            FormatId: CoreAudioFormatId.LinearPcm,
            FormatFlags: CoreAudioFormatFlags.IsFloat
                | CoreAudioFormatFlags.IsPacked
                | CoreAudioFormatFlags.IsNonInterleaved);

        Assert.Equal(new AudioFormat(48_000, 1, SampleKind.Float32), CoreAudioFormat.Classify(stream));
    }

    // CoreAudio's four-char codes are the ASCII packed big-endian into a UInt32.
    private static uint FourCharCode(string code) =>
        ((uint)code[0] << 24) | ((uint)code[1] << 16) | ((uint)code[2] << 8) | code[3];
}
