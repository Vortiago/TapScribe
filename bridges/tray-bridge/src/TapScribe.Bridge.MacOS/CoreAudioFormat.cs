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
    /// <exception cref="NotSupportedException">The stream is one the resampler cannot read.
    /// Not the seam's native-failure type: the device answered, and the answer was
    /// unusable, which is the distinction <see cref="IAudioDeviceEnumerator.Open"/> declares
    /// two exceptions for.</exception>
    public static AudioFormat Classify(CoreAudioStreamFormat stream)
    {
        ArgumentNullException.ThrowIfNull(stream);

        CoreAudioFormatFlags flags = stream.FormatFlags;
        bool readable =
            stream.FormatId == CoreAudioFormatId.LinearPcm
            && stream.SampleRate > 0
            && stream.ChannelsPerFrame > 0
            // Packed, or the sample stride is wider than the sample and every read after the
            // first lands mid-word. Little-endian, or every sample reads byte-reversed.
            && flags.HasFlag(CoreAudioFormatFlags.IsPacked)
            && !flags.HasFlag(CoreAudioFormatFlags.IsBigEndian)
            // A mono stream has nothing to interleave with, so the flag describes nothing and
            // the bytes are the same either way; above one channel it means separate buffers,
            // and the seam hands on one interleaved run.
            && (!flags.HasFlag(CoreAudioFormatFlags.IsNonInterleaved) || stream.ChannelsPerFrame == 1);

        SampleKind? kind = (readable, flags.HasFlag(CoreAudioFormatFlags.IsFloat), stream.BitsPerChannel) switch
        {
            (true, true, 32) => SampleKind.Float32,
            (true, false, 16) when flags.HasFlag(CoreAudioFormatFlags.IsSignedInteger) => SampleKind.Int16,
            _ => null,
        };

        return kind is null
            ? throw new NotSupportedException(
                $"Unsupported capture format: {stream.BitsPerChannel}-bit, {stream.ChannelsPerFrame} channel(s) " +
                $"at {stream.SampleRate:0.##} Hz, flags [{flags}]. Expected packed little-endian interleaved " +
                "linear PCM, either 32-bit float or 16-bit signed integer.")
            : new AudioFormat((int)stream.SampleRate, stream.ChannelsPerFrame, kind.Value);
    }
}
