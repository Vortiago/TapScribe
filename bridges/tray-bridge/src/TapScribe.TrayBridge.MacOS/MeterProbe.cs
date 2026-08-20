using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// One display-only level meter's worth of plumbing: an enumerator, a throwaway capture over
/// the device a caller picks, and the <see cref="InputLevelMeter"/> reading it (#421).
///
/// Its own class because the lifecycle is the whole difficulty and the Settings window has two
/// of them. Nothing here is on the tap path: this capture exists to move a bar, so every way it
/// can fail ends in a still bar and a message rather than in a broken window.
///
/// STARTED ON DEMAND, never when the window opens. A system-audio meter is a second process
/// tap, and reading audio through one needs the Screen and System Audio Recording grant, so
/// opening it eagerly would fire that prompt at an operator who came to Settings to correct a
/// hostname. Two simultaneous taps are permitted (proven in #420), so the constraint here is
/// the PROMPT's timing rather than the capability.
/// </summary>
/// <param name="openEnumerator">Opens an enumerator. Held for as long as the capture is, since
/// the capture came from it.</param>
/// <param name="pick">Chooses which listed device to meter, or null when the one this meter is
/// about is not present.</param>
internal sealed class MeterProbe(
    Func<IAudioDeviceEnumerator> openEnumerator,
    Func<IReadOnlyList<CaptureDevice>, CaptureDevice?> pick) : IDisposable
{
    private IAudioDeviceEnumerator? _enumerator;
    private InputLevelMeter? _meter;

    /// <summary>Why the meter is not running, or null. Shown next to the bar, because a still
    /// bar and a denied grant look identical.</summary>
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

            var meter = new InputLevelMeter(enumerator.Open(device));
            meter.Start();
            _meter = meter;
        }
        catch (Exception ex) when (CaptureSeam.IsDeclaredFailure(ex))
        {
            // The seam's whole declared set, named once in Core rather than listed here. Listing
            // it here is what shipped and it listed two of the four: the other two, an endpoint
            // gone between the List and the Open and a layout the pipeline cannot read, escaped
            // into an NSButton handler, which on AppKit ends the tray.
            //
            // Not a catch-all, though an escape here is that costly. A catch-all also swallows
            // what no device can produce, and reports a null dereference to the operator as a
            // level bar that does not move.
            Error = ex.Message;
            Stop();
        }
    }

    internal void Stop()
    {
        // The meter owns the capture and disposes it; the enumerator goes after, since the
        // capture came from it and a live capture over a disposed enumerator is exactly the
        // ownership mistake the seam's disposal contract exists to prevent.
        _meter?.Dispose();
        _meter = null;
        _enumerator?.Dispose();
        _enumerator = null;
    }

    public void Dispose() => Stop();
}
