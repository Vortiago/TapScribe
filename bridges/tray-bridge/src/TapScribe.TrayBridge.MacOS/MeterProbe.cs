using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// One level meter's plumbing: an enumerator, a throwaway capture, and the
/// <see cref="InputLevelMeter"/> reading it (#421). Its own class because the lifecycle is the
/// difficulty and Settings has two. Not on the tap path, so every failure ends in a still bar.
///
/// Started ON DEMAND. A system-audio meter is a second process tap and reading one needs the
/// Screen and System Audio Recording grant, so an eager meter would fire that prompt at someone
/// who came to fix a hostname. Two taps at once are fine (#420); the timing is the point.
/// </summary>
/// <param name="openEnumerator">Opens an enumerator. Outlives the capture it produced.</param>
/// <param name="pick">Which listed device to meter, or null when it is not present.</param>
internal sealed class MeterProbe(
    Func<IAudioDeviceEnumerator> openEnumerator,
    Func<IReadOnlyList<CaptureDevice>, CaptureDevice?> pick) : IDisposable
{
    private IAudioDeviceEnumerator? _enumerator;
    private InputLevelMeter? _meter;

    /// <summary>Why the meter is not running, or null. A still bar and a denied grant look
    /// identical without it.</summary>
    internal string? Error { get; private set; }

    /// <summary>The current RMS, or 0 when nothing is running.</summary>
    internal double Level => _meter?.Level ?? 0;

    internal bool Running => _meter is not null;

    internal void Start()
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

            // Published BEFORE the start, so every way the start can fail has an owner for what
            // Open already built. Assigned after it, a refused start left Stop with a null meter,
            // an undisposed capture and an enumerator disposed under it. It is the likeliest
            // failure of the whole feature: macOS asks for the System Audio Recording grant when
            // the IOProc starts, so a declined prompt lands exactly here.
            _meter = meter;
            meter.Start();
        }
        catch (Exception ex) when (CaptureSeam.IsDeclaredOpenFailure(ex))
        {
            // The seam's whole set, named once in Core: a subset lets the rest escape an
            // NSButton handler, which on AppKit ends the tray. Not a catch-all: that would
            // report a bug as a bar that will not move.
            Error = ex.Message;
            Stop();
        }
    }

    internal void Stop()
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
