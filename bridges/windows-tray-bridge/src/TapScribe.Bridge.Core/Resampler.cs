using System.Buffers.Binary;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Converts raw device-format PCM into the fixed `/tap` wire format:
/// <see cref="TapWire.SampleRate"/> Hz, mono, signed 16-bit little-endian.
///
/// This is the headline cross-platform piece: it lives in the core (no NAudio),
/// so it compiles and runs on the ubuntu CI runner and is exercised with
/// synthetic PCM. The capture backend hands us raw device samples; the core does
/// channel downmix + rate conversion + float/int16 normalisation.
///
/// Rate conversion is streaming linear interpolation. Per-call fractional
/// position and the previous block's last sample are carried across calls, so
/// chunk boundaries interpolate continuously (no clicks) and the output of N
/// small calls matches one big call. Linear interpolation is deliberately
/// modest for a tracer bullet; NAudio.Core's WdlResamplingSampleProvider
/// (cross-platform) is the higher-fidelity upgrade path if needed.
///
/// Not thread-safe: drive it from one thread (the capture callback).
/// </summary>
public sealed class Resampler
{
    private readonly AudioFormat _source;
    private readonly double _ratio; // source samples consumed per output sample

    // Carries an incomplete interleaved frame's bytes between calls so decoding
    // stays sample-aligned even if a backend hands us an odd-sized chunk.
    private byte[] _byteRemainder = [];

    // Linear-interpolation state in "working array" coordinates, where index -1
    // is the previous block's last mono sample (0 before the first block, which
    // is never read because _pos starts at 0).
    private double _pos;       // next output's source position
    private float _lastSample; // previous block's final mono sample (index -1)

    public Resampler(AudioFormat source)
    {
        if (source.SampleRate <= 0)
            throw new ArgumentOutOfRangeException(nameof(source), "Sample rate must be positive.");
        if (source.Channels <= 0)
            throw new ArgumentOutOfRangeException(nameof(source), "Channel count must be positive.");
        _source = source;
        _ratio = (double)source.SampleRate / TapWire.SampleRate;
    }

    /// <summary>The target format produced by <see cref="Process"/>.</summary>
    public static AudioFormat Target { get; } =
        new(TapWire.SampleRate, TapWire.Channels, SampleKind.Int16);

    /// <summary>
    /// Convert one chunk of device-format bytes to 16 kHz mono int16 PCM bytes.
    /// May return an empty array when there isn't yet enough input to advance.
    /// </summary>
    public byte[] Process(ReadOnlySpan<byte> deviceBytes)
    {
        float[] mono = DecodeToMono(deviceBytes);
        int m = mono.Length;
        if (m == 0)
            return [];

        // Resample mono[] (at source rate) -> output floats (at 16 kHz). We can
        // produce an output while its source position has both neighbours
        // available: i in [-1, m-2], i+1 in [0, m-1]. That holds while _pos < m-1.
        var output = new List<float>(1 + (int)(m / _ratio));
        while (_pos < m - 1)
        {
            int i = (int)Math.Floor(_pos);
            double frac = _pos - i;
            float left = i < 0 ? _lastSample : mono[i];
            float right = mono[i + 1];
            output.Add((float)(left + (right - left) * frac));
            _pos += _ratio;
        }

        // Shift coordinates so the next block's sample 0 follows this block's
        // last sample. _pos was >= m-1 when the loop exited, so _pos - m >= -1.
        _lastSample = mono[m - 1];
        _pos -= m;

        return EncodeInt16(output);
    }

    private float[] DecodeToMono(ReadOnlySpan<byte> deviceBytes)
    {
        int block = _source.BytesPerInterleavedFrame;

        // Combine any carried partial-frame bytes with the new chunk.
        byte[] work;
        if (_byteRemainder.Length == 0)
        {
            work = deviceBytes.ToArray();
        }
        else
        {
            work = new byte[_byteRemainder.Length + deviceBytes.Length];
            _byteRemainder.CopyTo(work, 0);
            deviceBytes.CopyTo(work.AsSpan(_byteRemainder.Length));
        }

        int frames = work.Length / block;
        int consumed = frames * block;

        var mono = new float[frames];
        for (int f = 0; f < frames; f++)
        {
            int baseOff = f * block;
            double sum = 0;
            for (int c = 0; c < _source.Channels; c++)
            {
                int off = baseOff + c * _source.BytesPerSample;
                sum += _source.Kind == SampleKind.Float32
                    ? BinaryPrimitives.ReadSingleLittleEndian(work.AsSpan(off, 4))
                    : BinaryPrimitives.ReadInt16LittleEndian(work.AsSpan(off, 2)) / 32768.0;
            }
            mono[f] = (float)(sum / _source.Channels);
        }

        // Stash the trailing partial interleaved frame (if any) for next time.
        _byteRemainder = work.AsSpan(consumed).ToArray();
        return mono;
    }

    private static byte[] EncodeInt16(List<float> samples)
    {
        var bytes = new byte[samples.Count * 2];
        for (int n = 0; n < samples.Count; n++)
        {
            int v = (int)Math.Round(samples[n] * 32767f);
            v = Math.Clamp(v, short.MinValue, short.MaxValue);
            BinaryPrimitives.WriteInt16LittleEndian(bytes.AsSpan(n * 2, 2), (short)v);
        }
        return bytes;
    }
}
