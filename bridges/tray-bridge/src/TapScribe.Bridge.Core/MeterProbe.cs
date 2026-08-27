namespace TapScribe.Bridge.Core;

/// <summary>
/// One level meter's plumbing: an enumerator, a throwaway capture, and the
/// <see cref="InputLevelMeter"/> reading it (#421). Its own class because the lifecycle is the
/// difficulty and each Settings dialog has two. Off the tap path, so a failure ends in a still
/// bar, never a lost meeting.
///
/// WHEN to start is the caller's, and the shells differ on purpose: Windows on open, macOS on
/// demand, where an eager system-audio meter would fire the Screen and System Audio Recording
/// prompt at someone who came to fix a hostname.
/// </summary>
/// <param name="openEnumerator">Opens an enumerator. Outlives the capture it produced.</param>
/// <param name="pick">Which listed device to meter, or null when it is not present.</param>
public sealed class MeterProbe(
    Func<IAudioDeviceEnumerator> openEnumerator,
    Func<IReadOnlyList<CaptureDevice>, CaptureDevice?> pick) : IDisposable
{
    private IAudioDeviceEnumerator? _enumerator;
    private InputLevelMeter? _meter;

    /// <summary>Why the meter is not running, or null. A still bar and a denied grant look
    /// identical without it.</summary>
    public string? Error { get; private set; }

    /// <summary>The current RMS, or 0 when nothing is running.</summary>
    public double Level => _meter?.Level ?? 0;

    public bool Running => _meter is not null;

    public void Start()
    {
        if (_meter is not null)
            return;

        Error = null;
        try
        {
            IAudioDeviceEnumerator enumerator = openEnumerator();
            _enumerator = enumerator;
            if (pick(enumerator.List()) is not { } device)
            {
                Error = "not present";
                Stop();
                return;
            }

            IAudioCapture capture = enumerator.Open(device);
            InputLevelMeter meter;
            try
            {
                meter = new InputLevelMeter(capture);
            }
            catch
            {
                // The meter did not take the capture, so this call still owns it: the ctor
                // reads the device format and refuses a layout the resampler cannot take,
                // which is the seam's NotSupportedException over a capture that opened fine.
                capture.Dispose();
                throw;
            }

            // Published BEFORE the start, so a failed start has an owner for what Open built.
            // Assigned after it, a refusal left Stop with a null meter, an undisposed capture and
            // an enumerator disposed under it. macOS answers the System Audio Recording prompt at
            // IOProc start, so a declined grant lands exactly here.
            _meter = meter;
            meter.Start();
        }
        catch (Exception ex) when (CaptureSeam.IsDeclaredOpenFailure(ex))
        {
            // The whole open set: a subset lets the rest escape into a UI event handler, which
            // on AppKit ends the tray. Not a catch-all, which would report a bug as a dead bar.
            Error = ex.Message;
            Stop();
        }
    }

    public void Stop()
    {
        // Meter first: it owns the capture, and a live capture over a disposed enumerator is the
        // ownership mistake the seam's disposal contract exists to prevent.
        _meter?.Dispose();
        _meter = null;
        _enumerator?.Dispose();
        _enumerator = null;
    }

    public void Dispose() => Stop();
}
