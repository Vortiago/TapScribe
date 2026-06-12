using System.Buffers.Binary;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

public class ResamplerTests
{
    private const int Wire = TapWire.SampleRate; // 16_000

    [Fact]
    public void Downsamples_48kStereoFloat_To16kMonoInt16_WithExpectedLengthAndFormat()
    {
        // 1.0 s of 48 kHz stereo float -> 16 kHz mono int16 == 16000 samples.
        byte[] input = InterleavedFloat(48_000, channels: 2, seconds: 1.0, (f, _) => Sine(f, 48_000, 440, 0.5f));
        var resampler = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32));

        byte[] output = resampler.Process(input);

        Assert.Equal(0, output.Length % 2);              // whole int16 samples
        Assert.Equal(Wire, output.Length / 2);           // exactly one second @ 16 kHz
        Assert.Contains(Samples(output), s => Math.Abs((int)s) > 1000); // real signal, not silence
    }

    [Fact]
    public void DownmixesChannels_OppositePhaseCancelsToSilence()
    {
        byte[] input = InterleavedFloat(48_000, channels: 2, seconds: 0.1, (_, c) => c == 0 ? 0.5f : -0.5f);
        var resampler = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32));

        short[] samples = Samples(resampler.Process(input));

        Assert.All(samples, s => Assert.Equal(0, s));
    }

    [Fact]
    public void DownmixesChannels_AveragesToMidScale()
    {
        // Both channels constant +0.5 -> mono 0.5 -> round(0.5 * 32767) == 16384.
        byte[] input = InterleavedFloat(48_000, channels: 2, seconds: 0.1, (_, _) => 0.5f);
        var resampler = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32));

        short[] samples = Samples(resampler.Process(input));

        Assert.NotEmpty(samples);
        Assert.All(samples, s => Assert.Equal(16384, s));
    }

    [Fact]
    public void Passthrough_16kMonoInt16_PreservesLengthAndValues()
    {
        // A ramp at the wire format: nothing should change but a sub-sample of
        // boundary handling. Output length within one sample of the input.
        const int n = 8_000;
        byte[] input = new byte[n * 2];
        for (int i = 0; i < n; i++)
            BinaryPrimitives.WriteInt16LittleEndian(input.AsSpan(i * 2, 2), (short)((i % 200) * 100 - 10_000));
        var resampler = new Resampler(new AudioFormat(Wire, 1, SampleKind.Int16));

        short[] output = Samples(resampler.Process(input));

        Assert.InRange(output.Length, n - 1, n);
        // Values track the input ramp (ratio 1.0 -> identity within rounding).
        for (int i = 0; i < output.Length; i++)
            Assert.True(Math.Abs(output[i] - ((i % 200) * 100 - 10_000)) <= 1, $"sample {i} drifted");
    }

    [Fact]
    public void StreamingInChunks_MatchesSinglePass_Exactly()
    {
        // 48k -> 16k is an exact 3:1 ratio, so every fractional position is 0 and
        // the resampler reduces to pure decimation: chunked output equals
        // single-pass output BYTE-FOR-BYTE. This exact equality is an
        // integer-ratio-only invariant (see the 44.1k test for the +/-1 LSB
        // contract on non-integer ratios). The odd split (4001 bytes) also
        // exercises the partial-interleaved-frame carry.
        byte[] input = InterleavedFloat(48_000, channels: 2, seconds: 0.1, (f, _) => Sine(f, 48_000, 300, 0.7f));

        byte[] single = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32)).Process(input);

        var chunked = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32));
        byte[] partA = chunked.Process(input.AsSpan(0, 4001));
        byte[] partB = chunked.Process(input.AsSpan(4001));
        byte[] joined = [.. partA, .. partB];

        Assert.Equal(single, joined);
    }

    [Fact]
    public void Streaming_NonIntegerRatio_44k1_MatchesSinglePass_WithinOneLsb()
    {
        // 44100 -> 16000 is the common consumer rate and a NON-integer ratio
        // (~2.756), so fractional interpolation is genuinely exercised. Exact
        // byte equality across chunks does NOT hold — cross-call FP rebasing can
        // drift a sample by at most 1 LSB — so the contract here is +/-1 LSB per
        // sample plus a correct length, not byte equality. Making it byte-exact
        // would be over-engineering a tracer bullet; ~-90 dB error is inaudible.
        byte[] input = InterleavedFloat(44_100, channels: 2, seconds: 0.1, (f, _) => Sine(f, 44_100, 300, 0.7f));

        short[] single = Samples(new Resampler(new AudioFormat(44_100, 2, SampleKind.Float32)).Process(input));

        var chunked = new Resampler(new AudioFormat(44_100, 2, SampleKind.Float32));
        byte[] partA = chunked.Process(input.AsSpan(0, 4001));
        byte[] partB = chunked.Process(input.AsSpan(4001));
        short[] joined = Samples([.. partA, .. partB]);

        Assert.InRange(single.Length, 1_599, 1_600); // 0.1 s @ 16 kHz, allow boundary carry
        Assert.Equal(single.Length, joined.Length);
        for (int i = 0; i < single.Length; i++)
            Assert.True(Math.Abs(single[i] - joined[i]) <= 1, $"sample {i} differs by more than 1 LSB");
    }

    [Fact]
    public void Silence_ProducesSilence()
    {
        byte[] input = new byte[48_000 * 2 * 4]; // 0.5 s of 48k stereo float zeros
        var resampler = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32));

        short[] samples = Samples(resampler.Process(input));

        Assert.NotEmpty(samples);
        Assert.All(samples, s => Assert.Equal(0, s));
    }

    [Fact]
    public void Upsamples_8kMono_To16k_RoughlyDoublesSampleCount()
    {
        byte[] input = InterleavedFloat(8_000, channels: 1, seconds: 0.5, (f, _) => Sine(f, 8_000, 200, 0.5f));
        var resampler = new Resampler(new AudioFormat(8_000, 1, SampleKind.Float32));

        short[] samples = Samples(resampler.Process(input));

        // 0.5 s @ 16 kHz ~= 8000 output samples (within a sample of the edges).
        Assert.InRange(samples.Length, 7_998, 8_000);
    }

    [Fact]
    public void Process_EmptyInput_ReturnsEmpty()
    {
        var resampler = new Resampler(new AudioFormat(48_000, 2, SampleKind.Float32));

        Assert.Empty(resampler.Process(ReadOnlySpan<byte>.Empty));
        Assert.Empty(resampler.Process(new byte[0]));
    }

    [Fact]
    public void Downmixes_Int16StereoInput_ByAveragingChannels()
    {
        // The float32 tests cover one decode path; this covers the int16 decode +
        // downmix. L=12000, R=4000 (int16) -> mono 8000 -> 8000 on the wire.
        const int frames = 4_800; // 0.1 s @ 48 kHz
        var input = new byte[frames * 2 * 2]; // 2 channels x int16
        for (int f = 0; f < frames; f++)
        {
            BinaryPrimitives.WriteInt16LittleEndian(input.AsSpan(f * 4, 2), 12_000);
            BinaryPrimitives.WriteInt16LittleEndian(input.AsSpan(f * 4 + 2, 2), 4_000);
        }
        var resampler = new Resampler(new AudioFormat(48_000, 2, SampleKind.Int16));

        short[] samples = Samples(resampler.Process(input));

        Assert.NotEmpty(samples);
        Assert.All(samples, s => Assert.True(Math.Abs(s - 8_000) <= 1, $"got {s}, expected ~8000"));
    }

    // --- helpers -----------------------------------------------------------

    private static float Sine(int frame, int rate, double freq, float amplitude) =>
        amplitude * (float)Math.Sin(2 * Math.PI * freq * frame / rate);

    private static byte[] InterleavedFloat(int rate, int channels, double seconds, Func<int, int, float> sample)
    {
        int frames = (int)(rate * seconds);
        var bytes = new byte[frames * channels * 4];
        int offset = 0;
        for (int f = 0; f < frames; f++)
        {
            for (int c = 0; c < channels; c++)
            {
                BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(offset, 4), sample(f, c));
                offset += 4;
            }
        }
        return bytes;
    }

    private static short[] Samples(byte[] pcm)
    {
        var samples = new short[pcm.Length / 2];
        for (int i = 0; i < samples.Length; i++)
            samples[i] = BinaryPrimitives.ReadInt16LittleEndian(pcm.AsSpan(i * 2, 2));
        return samples;
    }
}
