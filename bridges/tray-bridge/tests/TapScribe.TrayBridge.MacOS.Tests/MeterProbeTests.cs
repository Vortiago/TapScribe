using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// The Settings level meter's capture lifecycle (#421). The window around it is AppKit and
/// cannot be tested; <see cref="MeterProbe"/> touches no AppKit type at all, so the part that
/// decides anything is testable and belongs tested, the way SettingsSeed is.
///
/// What it decides: which native failures become which message, that a pick which finds nothing
/// stops cleanly rather than half-started, and that the enumerator outlives the capture it
/// produced. All three are invisible to an operator until the bar is dead or a device stays
/// busy after the window closed.
/// </summary>
public class MeterProbeTests
{
    private static readonly CaptureDevice Mic = new("mic-1", "Built-in Microphone", DeviceFlow.Capture, true);

    [Fact]
    public void Start_OverAPickedDevice_RunsWithNoError()
    {
        var enumerator = new Enumerator([Mic]);
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Start();

        Assert.True(probe.Running);
        Assert.Null(probe.Error);
    }

    [Fact]
    public void Start_WhenThePickFindsNothing_ReportsItAndLeavesNothingOpen()
    {
        // The device the meter is about is simply not present: a mic unplugged between opening
        // Settings and ticking the box. Distinct from a failure, and the operator needs the
        // difference, so it is its own message rather than an exception's.
        var enumerator = new Enumerator([]);
        using var probe = new MeterProbe(() => enumerator, static _ => null);

        probe.Start();

        Assert.False(probe.Running);
        Assert.Equal("not present", probe.Error);
        Assert.True(enumerator.Disposed, "the enumerator opened for a pick that missed was left open");
    }

    [Fact]
    public void Start_WhenTheDeviceRefusesToOpen_SurfacesTheMessageAndStaysStopped()
    {
        // The capture seam's declared failure, which on a Mac is most often a denied
        // microphone grant. Reported beside the bar rather than thrown: this runs from a
        // checkbox in a dialog whose other fields must stay editable.
        var enumerator = new Enumerator([Mic]) { OpenError = new ExternalException("opening mic-1", -66748) };
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Start();

        Assert.False(probe.Running);
        Assert.Contains("opening mic-1", probe.Error);
        Assert.True(enumerator.Disposed);
    }

    [Fact]
    public void Start_WhenTheEndpointVanishedBetweenTheListAndTheOpen_ReportsItRatherThanThrowing()
    {
        // The enumerator seam's declared clause for "the id names no active endpoint of the
        // requested flow" is ArgumentException, and the window between this probe's List and
        // its Open is two separate HAL walks wide. It runs from an NSButton handler, so an
        // escape here reaches AppKit and takes the tray with it.
        var enumerator = new Enumerator([Mic])
        {
            OpenError = new ArgumentException("no active capture endpoint with UID 'mic-1'", "device"),
        };
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Start();

        Assert.False(probe.Running);
        Assert.Contains("mic-1", probe.Error);
        Assert.True(enumerator.Disposed);
    }

    [Fact]
    public void Start_WhenTheDeviceLayoutIsUnreadable_ReportsItRatherThanThrowing()
    {
        // CoreAudioFormat.Classify's declared refusal, raised from the capture's own
        // constructor: a property of the operator's hardware rather than a race, so ticking
        // the box on such a Mac is deterministic. Same escape route as above.
        var enumerator = new Enumerator([Mic])
        {
            OpenError = new NotSupportedException("unsupported stream layout: 24-bit packed"),
        };
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Start();

        Assert.False(probe.Running);
        Assert.Contains("24-bit packed", probe.Error);
        Assert.True(enumerator.Disposed);
    }

    [Fact]
    public void Stop_DisposesTheCaptureBeforeItsEnumerator()
    {
        // The ordering the capture seam's disposal contract exists for: the capture came from
        // the enumerator, so a live capture over a disposed enumerator is use-after-free
        // wearing a managed type.
        var enumerator = new Enumerator([Mic]);
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);
        probe.Start();

        probe.Stop();

        Assert.False(probe.Running);
        Assert.Equal(0, probe.Level);
        Assert.True(enumerator.Capture!.Disposed, "the capture outlived its Stop");
        Assert.True(
            enumerator.Capture.DisposedBeforeEnumerator,
            "the enumerator went first, leaving a capture over a disposed owner");
    }

    [Fact]
    public void Start_Twice_OpensOneCapture()
    {
        // Ticking an already-ticked box, and the AppKit path that can produce it: StopMeters
        // unticks without asking, so a toggle handler can fire against a probe already running.
        var enumerator = new Enumerator([Mic]);
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Start();
        probe.Start();

        Assert.Equal(1, enumerator.Opens);
    }

    [Fact]
    public void Stop_WithoutAStart_IsSafe()
    {
        // Reached on every window close, whether or not a meter ever ran.
        var enumerator = new Enumerator([Mic]);
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Stop();

        Assert.False(probe.Running);
        Assert.Equal(0, enumerator.Opens);
    }

    private sealed class Enumerator(IReadOnlyList<CaptureDevice> devices) : IAudioDeviceEnumerator
    {
        internal Exception? OpenError { get; init; }

        internal bool Disposed { get; private set; }

        internal int Opens { get; private set; }

        internal Capture? Capture { get; private set; }

        public IReadOnlyList<CaptureDevice> List() => devices;

        public IAudioCapture Open(CaptureDevice device)
        {
            if (OpenError is not null)
                throw OpenError;
            Opens++;
            Capture = new Capture(this);
            return Capture;
        }

        public void Dispose() => Disposed = true;
    }

    private sealed class Capture(Enumerator owner) : IAudioCapture
    {
        internal bool Disposed { get; private set; }

        // Recorded at disposal rather than compared afterwards, because afterwards both are
        // disposed and the ORDER is the whole assertion.
        internal bool DisposedBeforeEnumerator { get; private set; }

        public AudioFormat Format => new(48_000, 1, SampleKind.Float32);

        public bool IsMuted => false;

        public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

        public event EventHandler<Exception?>? Failed;

        public event EventHandler? MuteChanged;

        public void Start()
        {
            // Nothing to produce: these tests are about the lifecycle, and a probe's Level is
            // the meter's business rather than this double's. Referencing the events keeps the
            // compiler from warning them unused without pretending they fire.
            _ = DataAvailable;
            _ = Failed;
            _ = MuteChanged;
        }

        public void Stop()
        {
        }

        public void Dispose()
        {
            DisposedBeforeEnumerator = !owner.Disposed;
            Disposed = true;
        }
    }
}
