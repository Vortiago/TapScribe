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

    [Theory]
    [MemberData(nameof(DeclaredOpenFailures))]
    public void Start_WhenOpenFails_ReportsItRatherThanThrowing(Exception refusal, string expected)
    {
        // One test for the seam's whole declared set, because the probe treats them as one path
        // and separate copies differing only in a type would say otherwise. The set is the
        // point: the filter listed two of these four, and the rest escaped an NSButton handler,
        // which on AppKit ends the tray rather than merely leaving a bar dead.
        var enumerator = new Enumerator([Mic]) { OpenError = refusal };
        using var probe = new MeterProbe(() => enumerator, devices => devices[0]);

        probe.Start();

        Assert.False(probe.Running);
        Assert.Contains(expected, probe.Error);
        Assert.True(enumerator.Disposed, "a refused open left its enumerator open");
    }

    // One row per <exception> tag on IAudioDeviceEnumerator.Open, so a tag added there without a
    // row here is a gap this file can be asked about.
    public static TheoryData<Exception, string> DeclaredOpenFailures() =>
        new()
        {
            // The platform refused the endpoint: on a Mac most often a denied microphone grant.
            { new ExternalException("opening mic-1", -66748), "opening mic-1" },
            // The id names no active endpoint of the requested flow. The window between this
            // probe's List and its Open is two separate HAL walks wide.
            { new ArgumentException("no active capture endpoint with UID 'mic-1'", "device"), "mic-1" },
            // No format the pipeline can take, raised from the capture's own constructor: a
            // property of the operator's hardware rather than a race, so it is deterministic.
            { new NotSupportedException("unsupported stream layout: 24-bit packed"), "24-bit packed" },
            // The endpoint cannot be opened in its current state.
            { new InvalidOperationException("device is not running"), "not running" },
        };

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
