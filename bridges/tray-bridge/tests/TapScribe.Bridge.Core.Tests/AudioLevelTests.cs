using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The shared RMS reading consumed by both the <see cref="LevelGate"/> (gating) and the
/// Settings input-level meter (display): one source of truth, so the meter's bar and the
/// gate's threshold can never drift onto different scales.
/// </summary>
public class AudioLevelTests
{
    [Fact]
    public void Rms_OfSilence_IsZero() =>
        Assert.Equal(0, AudioLevel.Rms(Fixtures.Silence(1)));

    // A constant (DC) frame's RMS is just its amplitude over full scale (32768), the
    // identity the gate fixtures lean on (value 8000 -> 0.244).
    [Theory]
    [InlineData((short)8000, 8000.0 / 32768)]
    [InlineData((short)16384, 0.5)]
    [InlineData((short)-16384, 0.5)]
    public void Rms_OfConstantFrame_IsAmplitudeOverFullScale(short value, double expected) =>
        Assert.Equal(expected, AudioLevel.Rms(Fixtures.Pcm(value, 1)), precision: 6);

    [Fact]
    public void Rms_OfEmptyBuffer_IsZero() =>
        Assert.Equal(0, AudioLevel.Rms([]));
}
