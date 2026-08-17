using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// Reduces a CoreAudio stream description to the <see cref="AudioFormat"/> the core
/// resampler consumes. Pure, and deliberately not a method on the capture class: the
/// Windows sibling keeps the same judgement inside a type that cannot be constructed
/// without a real endpoint, so its table of accepted layouts can only be exercised through
/// one. Here every layout a Mac can report is reachable from a test.
/// </summary>
public static class CoreAudioFormat
{
    /// <summary>The <see cref="AudioFormat"/> that <paramref name="stream"/> describes.
    /// </summary>
    /// <param name="stream">The device's current stream description.</param>
    /// <returns>The classified format; the raw bytes are unchanged, only the sample
    /// encoding is named.</returns>
    public static AudioFormat Classify(CoreAudioStreamFormat stream)
    {
        ArgumentNullException.ThrowIfNull(stream);
        SampleKind kind = stream.FormatFlags.HasFlag(CoreAudioFormatFlags.IsFloat)
            ? SampleKind.Float32
            : SampleKind.Int16;
        return new AudioFormat((int)stream.SampleRate, stream.ChannelsPerFrame, kind);
    }
}
