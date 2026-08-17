namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The <c>mFormatID</c> values this backend knows, as CoreAudio spells them: a
/// four-character code packed big-endian into a <c>UInt32</c>.
/// </summary>
public static class CoreAudioFormatId
{
    /// <summary><c>kAudioFormatLinearPCM</c>, the four-char code 'lpcm'. The only encoding a
    /// capture device hands an HAL client, and the only one the resampler can read.</summary>
    public const uint LinearPcm = 0x6C70636D;
}

/// <summary>
/// The <c>mFormatFlags</c> bits of a linear-PCM <c>AudioStreamBasicDescription</c>, named
/// as CoreAudio names them. Only the bits classification actually reads are declared;
/// an undeclared bit is one nothing here decides on.
/// </summary>
[Flags]
public enum CoreAudioFormatFlags : uint
{
    /// <summary>No flags set.</summary>
    None = 0,

    /// <summary><c>kAudioFormatFlagIsFloat</c>: samples are IEEE floats rather than integers.
    /// </summary>
    IsFloat = 1 << 0,

    /// <summary><c>kAudioFormatFlagIsBigEndian</c>. Every Mac this ships to is
    /// little-endian, and the core resampler reads little-endian bytes, so this bit set is a
    /// stream nothing here can read.</summary>
    IsBigEndian = 1 << 1,

    /// <summary><c>kAudioFormatFlagIsSignedInteger</c>: samples are signed integers.</summary>
    IsSignedInteger = 1 << 2,

    /// <summary><c>kAudioFormatFlagIsPacked</c>: every bit of each sample word carries
    /// data.</summary>
    IsPacked = 1 << 3,

    /// <summary><c>kAudioFormatFlagIsNonInterleaved</c>: each channel lives in its own
    /// buffer rather than interleaved in one. The seam hands out interleaved frames
    /// (<see cref="Core.AudioFormat.BytesPerInterleavedFrame"/>), so a multi-channel stream
    /// carrying this bit is one nothing here can read.</summary>
    IsNonInterleaved = 1 << 5,
}

/// <summary>
/// The decision-relevant fields of a CoreAudio <c>AudioStreamBasicDescription</c>, as a
/// plain record. Deliberately a faithful mirror rather than a friendlier shape: copying
/// the ASBD out is all the HAL does, so every judgement about what the fields MEAN stays
/// in <see cref="CoreAudioFormat"/> where a test can reach it without a device.
/// </summary>
/// <param name="SampleRate"><c>mSampleRate</c>, in Hz. A double because CoreAudio's is; a
/// device that is between formats can report 0.</param>
/// <param name="ChannelsPerFrame"><c>mChannelsPerFrame</c>.</param>
/// <param name="BitsPerChannel"><c>mBitsPerChannel</c>: 32 for float, 16 for the usual
/// integer stream.</param>
/// <param name="FormatId"><c>mFormatID</c>, see <see cref="CoreAudioFormatId"/>.</param>
/// <param name="FormatFlags"><c>mFormatFlags</c>, see <see cref="CoreAudioFormatFlags"/>.
/// </param>
public sealed record CoreAudioStreamFormat(
    double SampleRate,
    int ChannelsPerFrame,
    int BitsPerChannel,
    uint FormatId,
    CoreAudioFormatFlags FormatFlags);
