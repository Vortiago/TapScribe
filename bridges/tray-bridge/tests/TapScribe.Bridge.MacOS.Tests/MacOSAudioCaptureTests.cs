using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS <see cref="IAudioCapture"/> backend, driven entirely through
/// <see cref="FakeCoreAudioHal"/> (#419). No hardware, no TCC grant, no
/// <c>NSObject</c>: everything the class decides (mute policy, what a clean stop means,
/// which native error surfaces as what) is above the facade and therefore reachable from
/// the ubuntu lane, which is the whole point of the facade existing.
/// </summary>
public class MacOSAudioCaptureTests
{
    [Fact]
    public void Capture_OnADeviceWithNoMuteProperty_ReportsUnmutedAndWatchesNothing()
    {
        // The majority case for USB and virtual inputs: the endpoint carries no mute property
        // at all. Mirrors the Windows sibling, which degrades to no mute awareness rather than
        // failing the capture - the mic still records, it just cannot honour an OS mute, and
        // the level gate remains the only mute (#159).
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Podcast Mic"), mute: null);
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        int muteEvents = 0;
        capture.MuteChanged += (_, _) => muteEvents++;

        // Nothing to watch, so nothing was registered: the load-bearing half. Without it the
        // assertions below pass on a capture that registered a listener and simply never saw
        // it fire.
        Assert.Equal(0, hal.ListenerCount(device.ObjectId, CoreAudioPropertyKind.Mute));

        // And if the OS fires the property anyway, which it is entitled to do on a property
        // nobody asked about, it reaches nobody.
        hal.FireProperty(device.ObjectId, CoreAudioPropertyKind.Mute);

        Assert.False(capture.IsMuted);
        Assert.Equal(0, muteEvents);
    }

    [Fact]
    public void Capture_OnADeviceWithMuteSupport_RaisesMuteChangedOnEachTransition()
    {
        // Honouring the OS mute turns "muted" into a hard gate-closed, independent of level:
        // a muted mic still delivers a noise floor and DC offset, which is the recurring
        // "quiet" tap of #159. The event carries no payload, so IsMuted is the single source
        // of truth and the assertions read it rather than a captured argument.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: false);
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<bool> observed = [];
        capture.MuteChanged += (_, _) => observed.Add(capture.IsMuted);

        Assert.False(capture.IsMuted);

        hal.SetMuted(device.ObjectId, true);
        Assert.True(capture.IsMuted);

        hal.SetMuted(device.ObjectId, false);
        Assert.False(capture.IsMuted);

        Assert.Equal([true, false], observed);
    }

    [Fact]
    public void Capture_WhenThePropertyFiresWithoutTheMuteStateChanging_RaisesNothing()
    {
        // CoreAudio fires the mute property on the device's whole notification set, so a
        // volume tweak reaches the same listener. Forwarding only true transitions is what
        // keeps a volume slider from churning the pipeline, which is the same filter the
        // Windows sibling applies to OnVolumeNotification.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: true);
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        int muteEvents = 0;
        capture.MuteChanged += (_, _) => muteEvents++;

        // Seeded from the device, so a capture opened against an already-muted mic starts
        // closed rather than waiting for a transition that may never come.
        Assert.True(capture.IsMuted);

        hal.FireProperty(device.ObjectId, CoreAudioPropertyKind.Mute);

        Assert.True(capture.IsMuted);
        Assert.Equal(0, muteEvents);
    }

    [Fact]
    public void Capture_WhileStarted_SurfacesIoProcAudioAsDataAvailable()
    {
        // The whole point of the backend. The fake refuses to deliver into a device with no
        // RUNNING IOProc, so this also pins that Start actually created and started one
        // rather than leaving the callback wired to nothing.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<byte[]> received = [];
        // Copy on receipt: the seam says the buffer may be reused after the handler returns,
        // so retaining the Memory itself would be reading whatever the NEXT buffer holds.
        capture.DataAvailable += (_, e) => received.Add(e.Data.ToArray());

        capture.Start();
        hal.PushAudio(device.ObjectId, [1, 2, 3, 4, 5, 6, 7, 8]);
        hal.PushAudio(device.ObjectId, [9, 10]);

        Assert.Equal(new AudioFormat(48_000, 2, SampleKind.Float32), capture.Format);
        Assert.Equal([[1, 2, 3, 4, 5, 6, 7, 8], [9, 10]], received);
    }

    [Fact]
    public void Capture_OnACleanStop_RaisesFailedWithNoError()
    {
        // Failed is how the pipeline learns this device stopped delivering. A clean stop is
        // one of the ways that happens, and the seam spells it as a null payload precisely so
        // it does not read as "microphone lost - audio not being captured".
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);

        capture.Start();
        capture.Stop();

        Assert.Equal([null], failures);
        // Stopped AND unregistered. The fake refuses to destroy a running IOProc the way
        // CoreAudio does, so reaching zero on both counters is also the teardown ORDER.
        Assert.Equal(0, hal.RunningIoProcs);
        Assert.Equal(0, hal.LiveIoProcs);
    }

    [Fact]
    public void Capture_StoppedWhenItWasNeverStarted_RaisesNothing()
    {
        // The seam documents Stop as safe to call when not started, and teardown paths do
        // call it blind. Nothing was delivering, so there is no end-of-stream to announce:
        // a Failed here would have the pipeline report a device that never ran.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);

        capture.Stop();
        capture.Start();
        capture.Stop();
        capture.Stop();

        // Exactly one, from the one stop that ended a running stream.
        Assert.Equal([null], failures);
    }

    [Fact]
    public void Capture_DisposedWithoutStopping_ReleasesEverythingAndAnnouncesNothing()
    {
        // Dispose is not a stop being reported: by the time an owner releases the capture it
        // has already let go of the events, so raising Failed there would be a signal with
        // nobody to act on it. What Dispose owes is the release - the IOProc and the mute
        // listener - since the seam binds it to leave no handle behind.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: false);
        var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);
        capture.Start();

        capture.Dispose();

        Assert.Empty(failures);
        Assert.Equal(0, hal.RunningIoProcs);
        Assert.Equal(0, hal.LiveIoProcs);
        Assert.Equal(0, hal.LiveListeners);
    }

    [Fact]
    public void Capture_WhenTheDeviceRefusesToStart_ThrowsExternalExceptionAndLeavesNoIoProc()
    {
        // kAudioHardwareNotRunningError: the device is there but will not run, which is what
        // an endpoint already held exclusively answers. ExternalException is the seam's
        // declared failure type for a native error, and it is not decoration:
        // CaptureOrchestrator.StartAll filters on exactly it to skip a dead device without
        // sinking the meeting, so a platform-specific type leaking here sinks it.
        var hal = new FakeCoreAudioHal
        {
            StartIoError = new CoreAudioException("starting the IOProc on device 41", -66780),
        };
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);

        Assert.Throws<CoreAudioException>(capture.Start);
        Assert.IsAssignableFrom<ExternalException>(
            Record.Exception(capture.Start));

        // The registration is made before the start, so a device that refuses would otherwise
        // leak one per attempt, and the tray retries. Both attempts above cleaned up.
        Assert.Equal(0, hal.LiveIoProcs);
    }

    [Fact]
    public void Capture_WhenTheDeviceRefusesTheIoProc_ThrowsExternalException()
    {
        // The other native call Start makes. Same declared type, because the caller's question
        // is "did the platform refuse this endpoint", not "at which call".
        var hal = new FakeCoreAudioHal
        {
            CreateIoProcError = new CoreAudioException("creating an IOProc on device 41", -66748),
        };
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);

        Assert.IsAssignableFrom<ExternalException>(Record.Exception(capture.Start));
        Assert.Equal(0, hal.LiveIoProcs);
    }

    [Fact]
    public void Capture_StartedTwice_ThrowsInvalidOperationAndLeavesTheRunningStreamAlone()
    {
        // The seam declares InvalidOperationException for an already-started device,
        // deliberately NOT the native failure type: a double start is a bug in the caller,
        // not a dead endpoint, so the orchestrator's skip-and-carry-on filter must not
        // swallow it. Without the guard the second Start registers a second IOProc and
        // overwrites the handle, leaking the first for the process lifetime.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        capture.Start();

        Exception? thrown = Record.Exception(capture.Start);

        Assert.IsType<InvalidOperationException>(thrown);
        Assert.Equal(1, hal.LiveIoProcs);
        Assert.Equal(1, hal.RunningIoProcs);
    }
}
