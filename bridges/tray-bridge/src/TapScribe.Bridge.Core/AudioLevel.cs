using System.Buffers.Binary;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The RMS level of a chunk of 16 kHz mono int16 little-endian PCM, normalised to
/// [0, 1] (each sample divided by 32768). The one source of truth for "how loud is
/// this audio", shared by the <see cref="LevelGate"/> — which compares it to
/// <see cref="GateOptions.OpenThreshold"/> to gate utterances — and the Settings
/// dialog's input-level meter, which paints it against that same threshold. Keeping
/// both on this one reading is what lets the meter's bar and the gate's line share a
/// scale. RMS rather than peak so a single click can't spike the reading.
/// </summary>
public static class AudioLevel
{
    /// <summary>
    /// RMS of <paramref name="pcm"/> (int16 LE samples), normalised to [0, 1]. A
    /// trailing odd byte and an empty span both read as 0.
    /// </summary>
    public static double Rms(ReadOnlySpan<byte> pcm)
    {
        int samples = pcm.Length / 2;
        if (samples == 0)
            return 0;

        double sumSquares = 0;
        for (int i = 0; i < samples; i++)
        {
            double v = BinaryPrimitives.ReadInt16LittleEndian(pcm.Slice(i * 2, 2)) / 32768.0;
            sumSquares += v * v;
        }
        return Math.Sqrt(sumSquares / samples);
    }
}
