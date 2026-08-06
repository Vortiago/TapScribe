namespace TapScribe.Bridge.Core;

/// <summary>How raw capture samples are encoded in the byte buffer.</summary>
public enum SampleKind
{
    /// <summary>Signed 16-bit little-endian PCM (2 bytes/sample).</summary>
    Int16,

    /// <summary>32-bit IEEE float little-endian, range roughly [-1, 1] (4 bytes/sample).</summary>
    Float32,
}

/// <summary>
/// The format of the audio a capture device produces. The cross-platform
/// <see cref="Resampler"/> consumes this and converts to the fixed wire format
/// (<see cref="TapWire.SampleRate"/> mono int16). WASAPI shared-mode capture is
/// commonly 48 kHz / 2ch / <see cref="SampleKind.Float32"/>.
/// </summary>
public sealed record AudioFormat(int SampleRate, int Channels, SampleKind Kind)
{
    /// <summary>Bytes occupied by one sample of one channel.</summary>
    public int BytesPerSample => Kind == SampleKind.Float32 ? 4 : 2;

    /// <summary>Bytes occupied by one interleaved frame (all channels).</summary>
    public int BytesPerInterleavedFrame => BytesPerSample * Channels;
}
