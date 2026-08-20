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
    // A liveness bound, never a timing assertion: every use is "this must happen at all",
    // generous enough that a slow lane cannot fail it.
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

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
    public void Capture_DisposedTwice_ReleasesItsListenersOnce()
    {
        // The system-audio sibling guards this and the microphone did not, which is a
        // difference in the two captures' disposal contracts rather than in their needs: both
        // hand the same Registration objects back, and a Registration guards its pin with a
        // plain bool, so a repeat is a race over freeing a GCHandle rather than a no-op.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: false);
        var capture = new MacOSAudioCapture(hal, device.ObjectId);

        capture.Dispose();
        Assert.Null(Record.Exception(capture.Dispose));

        Assert.Equal(0, hal.LiveListeners);
    }

    [Fact]
    public void Capture_StartedAfterDispose_IsRefusedRatherThanRegisteringAnIoProc()
    {
        // Its two listeners are gone by then, so a capture started here would deliver audio
        // with nothing left to report a mute change or a vanished endpoint, and its IOProc
        // would outlive the only Dispose anyone was going to call. The sibling refuses it; this
        // one registered one.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: false);
        var capture = new MacOSAudioCapture(hal, device.ObjectId);
        capture.Dispose();

        Assert.Throws<ObjectDisposedException>(capture.Start);
        Assert.Equal(0, hal.LiveIoProcs);
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
        using var bothArrived = new CountdownEvent(2);
        // Copy on receipt: the seam says the buffer may be reused after the handler returns,
        // so retaining the Memory itself would be reading whatever the NEXT buffer holds.
        capture.DataAvailable += (_, e) =>
        {
            received.Add(e.Data.ToArray());
            bothArrived.Signal();
        };

        capture.Start();
        hal.PushAudio(device.ObjectId, [1, 2, 3, 4, 5, 6, 7, 8]);
        hal.PushAudio(device.ObjectId, [9, 10]);

        // Waited for rather than read straight after the push: delivery is off the IO thread
        // by design, so asserting inline would be a race this happens to win on a fast box.
        Assert.True(bothArrived.Wait(Wait), $"only {received.Count} of 2 buffers arrived");
        Assert.Equal(new AudioFormat(48_000, 2, SampleKind.Float32), capture.Format);
        Assert.Equal([[1, 2, 3, 4, 5, 6, 7, 8], [9, 10]], received);
    }

    [Fact]
    public async Task Capture_WhenAHandlerIsSlow_ReturnsTheIoThreadAnyway()
    {
        // The one property that makes this backend different from the Windows one. NAudio
        // raises DataAvailable on a managed thread it owns, which may allocate and block
        // freely; CoreAudio calls an IOProc on its realtime IO thread, which must return
        // inside the device's buffer period (about 10 ms at 48 kHz) or the device drops the
        // IOProc. The pipeline behind this event was written for the first shape: it
        // allocates several KB per buffer and takes locks that a threadpool thread and a
        // notification thread also take. Raising inline hands CoreAudio's deadline to all of
        // that, so the raise happens off the IO thread and this is what says so.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var wedge = new WedgedPump(hal, device);

        // Driven from another thread only so a regression FAILS rather than deadlocking the
        // run: an inline raise leaves PushAudio inside the blocked handler forever.
        Task push = Task.Run(() => hal.PushAudio(device.ObjectId, [1, 2, 3, 4]));

        Assert.True(wedge.HandlerEntered.Wait(Wait), "the handler never ran");
        await push.WaitAsync(Wait);
    }

    [Fact]
    public void Capture_WhenThePumpFallsBehind_DropsBuffersRatherThanStallingTheIoThread()
    {
        // The other half of not blocking: a ring that is full has to answer somehow, and the
        // only two options on this thread are overwrite a slot the pump is reading or drop.
        // Dropping is the honest one and is counted, so a machine that cannot keep up is a
        // number rather than a mystery. Nothing else exercises this branch, and an unreachable
        // guard in a realtime path is worth as much as no guard.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var wedge = new WedgedPump(hal, device);

        // Wedge the pump inside the first buffer, then hand the producer far more than the
        // ring holds. Every push returns, which is the property under test.
        hal.PushAudio(device.ObjectId, [1]);
        Assert.True(wedge.HandlerEntered.Wait(Wait), "the pump never picked up the first buffer");
        for (int i = 0; i < 64; i++)
            hal.PushAudio(device.ObjectId, [(byte)i]);

        Assert.True(
            wedge.Capture.DroppedBuffers > 0,
            "a full ring did not drop anything, so the guard is unreachable");
    }

    [Fact]
    public void Capture_HandedABufferLargerThanASlot_DropsItRatherThanAllocating()
    {
        // The producer's other refusal, and the one that would be silent: a device whose period
        // exceeds MaxBufferFrames drops EVERY buffer, so the capture runs and delivers nothing.
        // Allocating a bigger slot on this thread is the one thing it may not do, so the count
        // is the only way that device would ever be diagnosed. Deliverable as a test only
        // because a slot is now a device period rather than a whole second of audio.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        int delivered = 0;
        capture.DataAvailable += (_, _) => Interlocked.Increment(ref delivered);
        capture.Start();

        // 8192 frames of 48 kHz stereo float32 is the ceiling; this is one frame past it.
        hal.PushAudio(device.ObjectId, new byte[(8192 * 2 * 4) + 8]);

        Assert.Equal(1, capture.DroppedBuffers);
        Assert.Equal(0, delivered);
        capture.Stop();
    }

    /// <summary>A started capture whose pump is held inside its first handler, so a test can
    /// watch what the PRODUCER does while the consumer is stuck.
    ///
    /// It exists to own one ordering that is easy to get wrong and expensive when it is: the
    /// handler is released and the pump joined BEFORE the events it waits on are disposed.
    /// Get that backwards, or leave the release on a path an assertion can skip, and a
    /// background thread touches a disposed <see cref="ManualResetEventSlim"/>, which aborts
    /// the whole test host rather than failing one test.</summary>
    private sealed class WedgedPump : IDisposable
    {
        private readonly ManualResetEventSlim _release = new();

        public ManualResetEventSlim HandlerEntered { get; } = new();

        public MacOSAudioCapture Capture { get; }

        public WedgedPump(FakeCoreAudioHal hal, CoreAudioDevice device)
        {
            Capture = new MacOSAudioCapture(hal, device.ObjectId);
            Capture.DataAvailable += (_, _) =>
            {
                HandlerEntered.Set();
                _release.Wait(Wait);
            };
            Capture.Start();
        }

        public void Dispose()
        {
            // Release first, then join, then dispose: the reverse of construction, and the only
            // order in which nothing is inside the handler when the events go away. Runs from a
            // `using`, so a failing assertion takes this path too.
            _release.Set();
            Capture.Dispose();
            HandlerEntered.Dispose();
            _release.Dispose();
        }
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
    public void Capture_WhenTheDeviceGoesAwayMidStream_RaisesFailedWithTheReason()
    {
        // The mic was unplugged, disabled, or its interface went to sleep. CoreAudio simply
        // stops calling the IOProc, so without this the meeting keeps running and records
        // nothing under that speaker for the rest of the call, with the status line still
        // saying it is streaming. Failed carries a non-null payload precisely to read as
        // "microphone lost" rather than as the clean stop a null one means.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);
        capture.Start();

        hal.FireProperty(device.ObjectId, CoreAudioPropertyKind.DeviceIsAlive);

        Assert.IsAssignableFrom<ExternalException>(Assert.Single(failures));
    }

    [Fact]
    public void Capture_WhenTheDeviceGoesAwayBeforeAnythingStarted_RaisesNothing()
    {
        // The seam says Failed means capture ended unexpectedly MID-STREAM. A device that
        // leaves while nothing is capturing has ended no stream: the next Start will fail on
        // its own and the enumerator will not list it, which are the two places that fact
        // belongs. Raising here would have the pipeline report a device that never ran.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);

        hal.FireProperty(device.ObjectId, CoreAudioPropertyKind.DeviceIsAlive);

        Assert.Empty(failures);
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
        List<Exception?> failures = [];
        // Scoped rather than method-scoped, because disposing IS the act here and the
        // assertions below have to run AFTER it. A bare `using var` would dispose at the end
        // of the method, i.e. after they had already read the handle counts.
        {
            using var capture = new MacOSAudioCapture(hal, device.ObjectId);
            capture.Failed += (_, e) => failures.Add(e);
            capture.Start();
        }

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
    public void Capture_WhenTheDeviceRefusesTheIoProc_LeavesNoPumpThreadBehind()
    {
        // The pump is started BEFORE the IOProc, so the first buffer has somewhere to go,
        // which makes tearing it down part of every way Start can fail. A pump left parked on
        // a semaphore nothing will ever release is not collectable, and Dispose cannot reach
        // it either: it releases through the IOProc handle, which a failed Start never
        // assigns. The tray retries a device that refused, so it is one thread and one ring
        // per attempt for the process lifetime.
        //
        // Asserted on the pump directly rather than on collectability. A WeakReference plus
        // GC.Collect says the same thing, but says it about the collector: it needs the
        // capture out of every frame, so it needs an uninlined helper, and a failure reads as
        // "something still roots this" rather than naming the pump.
        var hal = new FakeCoreAudioHal
        {
            CreateIoProcError = new CoreAudioException("creating an IOProc on device 41", -66748),
        };
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var capture = new MacOSAudioCapture(hal, device.ObjectId);

        Assert.IsAssignableFrom<ExternalException>(Record.Exception(capture.Start));

        Assert.False(
            capture.IsPumping,
            "a Start that was refused left its pump thread parked on a semaphore nobody will release");
    }

    [Fact]
    public void Capture_WhenSeedingTheMuteStateIsRefused_LeavesNoListenerBehind()
    {
        // The constructor's bargain: a throw leaves this instance owning nothing. It has to
        // hold, because a ctor that throws hands the instance to NOBODY, so nobody can ever
        // Dispose it. The subscription is deliberately taken BEFORE the seeding read, so a
        // toggle during construction is not lost in the gap, and that makes the seed the one
        // call that can fail with something already owned: a native registration plus the
        // GCHandle rooting it, still firing into a half-constructed capture, for the process
        // lifetime.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: false);
        hal.SeedMuteError = new CoreAudioException("reading the mute state of device 41", -66748);

        Assert.IsAssignableFrom<ExternalException>(
            Record.Exception(() => new MacOSAudioCapture(hal, device.ObjectId)));

        Assert.Equal(0, hal.LiveListeners);
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
