using System.Buffers.Binary;
using TapScribe.Bridge.Core;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// What a macOS system-audio tap actually hands the pipeline: 48 kHz, stereo, 32-bit float
/// (#420). The core has no idea it is a tap - it is one more <see cref="IAudioCapture"/> - but
/// nothing before this exercised that FORMAT end to end, and it is the one every Mac reports.
///
/// Driven through <see cref="FakeAudioCapture"/> rather than through CoreAudio, so the claim is
/// about the resample, the gate and the wire, which is where a mix-format bug would actually
/// live. The macOS assembly's own tests prove the tap reports this format; this proves the far
/// side of a meeting survives it.
/// </summary>
public class SystemAudioMixTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    /// <summary>What almost every Mac's process tap reports.</summary>
    private static AudioFormat MixFormat => new(48_000, 2, SampleKind.Float32);

    // A steady level in both channels, as interleaved 32-bit floats. Constant rather than a
    // tone because the resampler interpolates linearly: a constant survives it exactly, so the
    // amplitude assertion below is about the float-to-int16 conversion and the downmix rather
    // than about interpolation error.
    private static byte[] StereoMix(float level, int frames)
    {
        var bytes = new byte[frames * MixFormat.BytesPerInterleavedFrame];
        for (int i = 0; i < frames * MixFormat.Channels; i++)
            BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(i * 4, 4), level);
        return bytes;
    }

    [Fact]
    public async Task AMacsStereoFloatMix_ArrivesAtTheRecorderAsWireFormatPcm()
    {
        // The whole far-side path in one test: 48 kHz stereo float in, wire-format mono int16
        // out, through the level gate that decides what is speech. Three ways this silently
        // breaks and each is checked, because every one of them produces a WAV that plays as
        // SOMETHING rather than nothing: the wrong rate (a meeting transcribed three times too
        // fast), a downmix that reads interleaved pairs as consecutive samples (the right
        // duration at half pitch), and a float path that reinterprets the bytes as integers
        // (full-scale noise the transcriber answers with confident nonsense).
        var transport = new FakeTapTransport();
        var system = new FakeAudioCapture(MixFormat);

        await using CaptureOrchestrator orchestrator = CaptureOrchestrator.StartAll(
            new CaptureSet([Spec(system, "System audio")]),
            onConnected: _ => { },
            onFailed: (_, _) => { },
            gate: FastGate(), stream: FastStream(), connectionFactory: transport.Create);

        // Half scale, which is a loud meeting and comfortably over the gate's threshold: a mix
        // the gate closed on would arrive as no frames at all, which is the far side of every
        // call recorded as silence.
        system.Emit(StereoMix(0.5f, frames: MixFormat.SampleRate / 2)); // half a second, at the tap's rate
        await orchestrator.DrainAllAsync().WaitAsync(Wait);

        byte[] delivered = transport.StreamedAudio("System audio");
        // Half a second at the WIRE's rate, allowing the gate's hangover to carry a few frames
        // of trailing silence past the audio. Bounded rather than exact, because how the gate
        // closes an utterance is the gate's business and is pinned by its own tests; what this
        // one is about is that the sample COUNT was divided by three and not left at 48 kHz.
        int samples = delivered.Length / 2;
        Assert.InRange(samples, TapWire.SampleRate / 2, TapWire.SampleRate);

        // Every sample at the level the mix carried, converted rather than reinterpreted: half
        // of full scale as a signed 16-bit sample. Read a frame into the audio, so neither the
        // gate's pre-roll nor its hangover is what is being measured.
        short middle = BinaryPrimitives.ReadInt16LittleEndian(delivered.AsSpan(TapWire.FrameBytes, 2));
        Assert.InRange(middle, (short)(short.MaxValue * 0.45), (short)(short.MaxValue * 0.55));
    }
}
