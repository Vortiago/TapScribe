namespace TapScribe.Bridge.Core;

/// <summary>
/// Splits a stream of 16 kHz mono int16 PCM bytes into exact
/// <see cref="TapWire.FrameBytes"/> (640-byte / 20 ms) frames, buffering the
/// trailing partial frame for the next push so frames stay aligned. Mirrors the
/// local-test-bridge's chunk_into_frames helper. One frame == one binary
/// WebSocket message on the wire.
///
/// Not thread-safe: drive it from one thread (the capture callback).
/// </summary>
public sealed class FrameChunker
{
    private byte[] _pending = [];

    /// <summary>
    /// Append <paramref name="pcm"/> and return every complete 640-byte frame
    /// now available. The leftover tail (&lt; 640 bytes) is retained internally.
    /// </summary>
    public List<byte[]> Push(ReadOnlySpan<byte> pcm)
    {
        byte[] combined;
        if (_pending.Length == 0)
        {
            combined = pcm.ToArray();
        }
        else
        {
            combined = new byte[_pending.Length + pcm.Length];
            _pending.CopyTo(combined, 0);
            pcm.CopyTo(combined.AsSpan(_pending.Length));
        }

        int frameCount = combined.Length / TapWire.FrameBytes;
        var frames = new List<byte[]>(frameCount);
        for (int i = 0; i < frameCount; i++)
            frames.Add(combined[(i * TapWire.FrameBytes)..((i + 1) * TapWire.FrameBytes)]);

        int consumed = frameCount * TapWire.FrameBytes;
        _pending = combined[consumed..];
        return frames;
    }

    /// <summary>Bytes currently buffered awaiting a full frame (0..639).</summary>
    public int PendingBytes => _pending.Length;

    /// <summary>
    /// Drop the buffered partial frame, so the next <see cref="Push"/> starts
    /// frame-aligned from scratch. Used when the stream is interrupted (e.g. the device
    /// mutes mid-utterance, #159) and a pre-interruption partial frame must not be
    /// stitched onto the resumed audio.
    /// </summary>
    public void Reset() => _pending = [];
}
