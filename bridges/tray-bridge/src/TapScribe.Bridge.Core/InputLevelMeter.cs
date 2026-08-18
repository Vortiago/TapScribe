using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The display-only level sampler behind the Settings dialog's per-device input-level
/// meter (issue #152). It rides one <see cref="IAudioCapture"/> opened purely for
/// metering — a second, independent shared-mode capture, so it never disturbs the
/// tap/gate pipeline — resamples each device-format chunk to the gate's 16 kHz mono int16
/// scale (reusing <see cref="Resampler"/> + <see cref="FrameChunker"/>), and reads each
/// 640-byte frame's level with <see cref="AudioLevel.Rms"/> — the same reading the gate
/// uses, so the meter's bar and the gate's threshold share one scale.
///
/// The reading is shaped into a VU-style envelope: instant attack (a loud frame shows at
/// once) and a per-frame exponential release (the bar falls back smoothly when sound
/// stops), so it's readable rather than flickering at the 20 ms frame rate. The capture
/// backend raises <see cref="IAudioCapture.DataAvailable"/> on its own thread; the level
/// is published as a torn-read-safe atomic that the UI thread polls in
/// <see cref="Level"/> — sampling never blocks the UI thread. Drive the capture callback
/// from one thread (the contract <see cref="Resampler"/>/<see cref="FrameChunker"/> assume).
/// </summary>
public sealed class InputLevelMeter : IDisposable
{
    /// <summary>Per-frame (20 ms) release factor: the held level is multiplied by this on
    /// each frame the input sits below it. ≈0.85 falls ~3 dB/frame, so a peak decays to the
    /// floor in ~0.3 s — a readable VU release without lagging real speech.</summary>
    public const double DefaultRelease = 0.85;

    private readonly IAudioCapture _capture;
    private readonly Resampler _resampler;
    private readonly FrameChunker _chunker = new();
    private readonly double _release;

    // Capture-thread envelope state (single writer: the DataAvailable handler).
    private double _held;
    // The published level, as int64 bits, so the UI-thread reader never sees a torn double.
    private long _levelBits;
    // Set on Dispose so a capture-thread callback already dispatched (the backend stops its
    // thread asynchronously) bails instead of writing a stale level after teardown.
    private volatile bool _disposed;

    public InputLevelMeter(IAudioCapture capture, double release = DefaultRelease)
    {
        ArgumentNullException.ThrowIfNull(capture);
        if (release is < 0 or >= 1 || double.IsNaN(release))
            throw new ArgumentOutOfRangeException(nameof(release), "Release must be in [0, 1).");
        _capture = capture;
        _release = release;
        _resampler = new Resampler(capture.Format);
        _capture.DataAvailable += OnDataAvailable;
    }

    /// <summary>The current level on the gate's RMS scale ([0, 1]); 0 before any audio.
    /// Safe to read from any thread (e.g. a UI timer).</summary>
    public double Level => BitConverter.Int64BitsToDouble(Interlocked.Read(ref _levelBits));

    /// <summary>Begin metering. <see cref="Level"/> updates until the meter is disposed.</summary>
    public void Start() => _capture.Start();

    public void Dispose()
    {
        _disposed = true;
        _capture.DataAvailable -= OnDataAvailable;
        try
        {
            _capture.Stop();
        }
        catch (Exception ex) when (ex is ExternalException or InvalidOperationException)
        {
            // The endpoint was invalidated while the meter ran: unplug the mic with Settings
            // open and stopping it raises the seam's declared native failure. There is nothing
            // left to stop, and the one thing that still matters - RELEASING the device below -
            // must not be skipped over it, or the dialog strands an endpoint every time a
            // device goes away. Swallowed rather than surfaced because this is display-only
            // teardown with no caller who could act on it. The filter is what the capture seam
            // lets Stop raise, and it is the same one TapSession.DisposeAsync applies.
            //
            // Reachable in practice on macOS, where the backend propagates the invalidation;
            // the WASAPI one swallows it inside its own Stop, so a Windows host never gets
            // here.
        }

        _capture.Dispose();
    }

    private void OnDataAvailable(object? sender, AudioCapturedEventArgs e)
    {
        if (_disposed)
            return;
        byte[] pcm = _resampler.Process(e.Data.Span);
        foreach (byte[] frame in _chunker.Push(pcm))
        {
            double rms = AudioLevel.Rms(frame);
            // Instant attack, exponential release: jump up to a louder frame at once, ease
            // down otherwise. Publish atomically for the UI-thread reader.
            _held = Math.Max(rms, _held * _release);
            Interlocked.Exchange(ref _levelBits, BitConverter.DoubleToInt64Bits(_held));
        }
    }
}
