using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS system-audio <see cref="IAudioCapture"/>: a Core Audio process tap inside a
/// private aggregate device (#420). Driven entirely through <see cref="FakeCoreAudioHal"/>,
/// which validates handle lifetime and refuses the orderings CoreAudio refuses, so the three
/// objects' composition and teardown are exercised on a lane with no audio hardware and no
/// TCC grant.
///
/// This is the half of a meeting the Bridge exists for: the operator's microphone is one
/// speaker, and what the Mac PLAYS is everyone else.
/// </summary>
public class MacOSSystemAudioCaptureTests
{
    // A liveness bound, never a timing assertion: every use is "this must happen at all",
    // generous enough that a slow lane cannot fail it.
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static FakeCoreAudioHal WithSpeakers(string name = "MacBook Pro Speakers")
    {
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Output(63, name, isDefault: true));
        return hal;
    }

    [Fact]
    public void SystemAudio_WhileStarted_HoldsOneTapOneAggregateAndOneRunningIoProc()
    {
        // The whole shape of the platform in one assertion. There is no loopback endpoint to
        // open on macOS: a tap is an object with no audio path, an aggregate device is what
        // gives it an AudioObjectID, and only then is there something an IOProc can run over.
        // Counted rather than merely "it started", because each of the three is a separate
        // native lifetime and any one of them left unmade is a capture that delivers nothing.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);

        capture.Start();

        Assert.Equal(1, hal.LiveTaps);
        Assert.Equal(1, hal.LiveAggregates);
        Assert.Equal(1, hal.LiveIoProcs);
        Assert.Equal(1, hal.RunningIoProcs);
    }

    [Fact]
    public void SystemAudio_AfterDispose_LeavesNoLiveHandleBehind()
    {
        // Three native objects and a listener, released in an order CoreAudio enforces: the
        // IOProc before the aggregate that carries it, the aggregate before the tap it lists.
        // The fake refuses each of those backwards, so reaching zero on every counter is the
        // ORDER as much as the count. A tap left behind is a private aggregate device sitting
        // in the operator's Mac until they log out.
        FakeCoreAudioHal hal = WithSpeakers();
        // Scoped rather than method-scoped, because disposing IS the act here and the
        // assertions below have to run AFTER it.
        {
            using var capture = new MacOSSystemAudioCapture(hal);
            capture.Start();
        }

        Assert.Equal(0, hal.LiveTaps);
        Assert.Equal(0, hal.LiveAggregates);
        Assert.Equal(0, hal.LiveIoProcs);
        Assert.Equal(0, hal.RunningIoProcs);
        Assert.Equal(0, hal.LiveListeners);
    }

    [Fact]
    public void SystemAudio_BindsTheAggregateToTheEndpointTheMacIsPlayingThrough()
    {
        // "System audio" means what the Mac is PLAYING, and what it is playing goes to the
        // default output by definition. An aggregate built around any other endpoint records
        // silence, which is why this capture is not opened against whichever render device it
        // was handed: it finds the default itself, through the same rule the meeting's
        // follow-default selection resolves by.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Output(63, "MacBook Pro Speakers"));
        hal.AddDevice(Devices.Output(71, "External Headphones", isDefault: true));
        using var capture = new MacOSSystemAudioCapture(hal);

        Assert.Equal(["External Headphones:uid"], hal.LiveAggregateOutputs);
        Assert.NotNull(capture);
    }

    [Fact]
    public void SystemAudio_OnAMacWithNoOutputAtAll_RefusesToOpenRatherThanTapNothing()
    {
        // A Mac with every output unplugged (a headless mini between reboots is the real one).
        // ExternalException is the capture seam's declared native failure, which is what
        // BridgeRuntime filters on to skip a device and record the meeting on the rest: the
        // microphone still has to make it into the session.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"));

        Assert.IsAssignableFrom<ExternalException>(
            Record.Exception(() => new MacOSSystemAudioCapture(hal)));

        // And nothing was left behind by the refusal: a ctor that throws hands the instance to
        // nobody, so nobody can ever Dispose it.
        Assert.Equal(0, hal.LiveTaps);
        Assert.Equal(0, hal.LiveAggregates);
        Assert.Equal(0, hal.LiveListeners);
    }

    [Fact]
    public void SystemAudio_WhenTheMacRefusesTheTap_SurfacesTheNativeFailureAndHoldsNothing()
    {
        // What a missing "System Audio Recording" grant looks like from here, and what a Mac
        // below the 14.4 floor would look like if one got this far. Same declared type, for
        // the same reason: the caller's question is "did the platform refuse", not "at which
        // call".
        FakeCoreAudioHal hal = WithSpeakers();
        hal.CreateProcessTapError = new CoreAudioException("creating a system-audio process tap", -66748);

        Assert.IsAssignableFrom<ExternalException>(
            Record.Exception(() => new MacOSSystemAudioCapture(hal)));

        Assert.Equal(0, hal.LiveTaps);
        Assert.Equal(0, hal.LiveAggregates);
        Assert.Equal(0, hal.LiveListeners);
    }

    [Fact]
    public void SystemAudio_WhenTheAggregateIsRefused_ReleasesTheTapItAlreadyMade()
    {
        // The one ctor step that can fail with something already owned. A tap with no
        // aggregate is invisible to every counter an operator or a developer can read, and it
        // survives for the process lifetime, so the unwind is the whole claim here.
        FakeCoreAudioHal hal = WithSpeakers();
        hal.CreateAggregateDeviceError = new CoreAudioException("creating the aggregate device", -66748);

        Assert.IsAssignableFrom<ExternalException>(
            Record.Exception(() => new MacOSSystemAudioCapture(hal)));

        Assert.Equal(0, hal.LiveTaps);
    }

    [Fact]
    public void SystemAudio_ReportsTheTapsOwnFormat_NotTheOutputDevices()
    {
        // The tap is a stereo mixdown that CoreAudio resamples for us, and its format is a
        // property of the TAP object rather than of the endpoint underneath. Reading the
        // wrong one is how a pipeline ends up resampling 48 kHz stereo float as though it
        // were whatever the speakers happen to be configured for.
        FakeCoreAudioHal hal = WithSpeakers();
        hal.TapFormat = Formats.Float32Stereo48k;
        using var capture = new MacOSSystemAudioCapture(hal);

        Assert.Equal(new AudioFormat(48_000, 2, SampleKind.Float32), capture.Format);
    }

    [Fact]
    public void SystemAudio_ReportsUnmuted_BecauseATapHasNoOsMuteToHonour()
    {
        // Matching the Windows loopback sibling: a render path has no mute event, so the level
        // gate is the only mute there is (#159). Asserted rather than left implicit, because
        // "muted" would hard-close the gate and record the far side of every meeting as
        // silence.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);

        Assert.False(capture.IsMuted);
        // And it watches no mute property, so there is nothing that could ever flip it.
        Assert.Equal(0, hal.ListenerCount(CoreAudioObject.System, CoreAudioPropertyKind.Mute));
    }

    [Fact]
    public void SystemAudio_WhileStarted_SurfacesTapAudioAsDataAvailable()
    {
        // The point of the whole file. The fake refuses to deliver into a device with no
        // RUNNING IOProc, so this also pins that Start registered and started one on the
        // AGGREGATE rather than leaving the callback wired to the tap, which is not a device.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<byte[]> received = [];
        using var bothArrived = new CountdownEvent(2);
        // Copy on receipt: the seam says the buffer may be reused after the handler returns.
        capture.DataAvailable += (_, e) =>
        {
            received.Add(e.Data.ToArray());
            bothArrived.Signal();
        };

        capture.Start();
        hal.PushAudio(capture.AggregateDeviceId, [1, 2, 3, 4]);
        hal.PushAudio(capture.AggregateDeviceId, [5, 6]);

        // Waited for rather than read straight after the push: delivery is off the IO thread
        // by design, so asserting inline would be a race this happens to win on a fast box.
        Assert.True(bothArrived.Wait(Wait), $"only {received.Count} of 2 buffers arrived");
        Assert.Equal([[1, 2, 3, 4], [5, 6]], received);
    }

    [Fact]
    public void SystemAudio_AfterDispose_HasNoHandleLeftForCoreAudioToDeliverInto()
    {
        // The other half of the release. A destroyed aggregate cannot be delivered into, and
        // the fake refuses to model one that could: a capture whose IOProc outlived its owner
        // is a meeting still streaming PCM after the operator ended it.
        FakeCoreAudioHal hal = WithSpeakers();
        var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();
        uint aggregate = capture.AggregateDeviceId;
        capture.Dispose();

        Assert.Throws<InvalidOperationException>(() => hal.PushAudio(aggregate, [1, 2, 3, 4]));
    }

    [Fact]
    public void SystemAudio_OnACleanStop_RaisesFailedWithNoError()
    {
        // Failed is how the pipeline learns this capture stopped delivering, and the seam
        // spells a clean stop as a null payload precisely so it does not read as "system audio
        // lost". Same contract as the microphone's, because the pipeline above cannot tell the
        // two backends apart.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);

        capture.Start();
        capture.Stop();

        Assert.Equal([null], failures);
        Assert.Equal(0, hal.RunningIoProcs);
        Assert.Equal(0, hal.LiveIoProcs);
        // The tap and its aggregate outlive a Stop: the capture is stopped, not released, and
        // the seam allows a Start after it.
        Assert.Equal(1, hal.LiveTaps);
        Assert.Equal(1, hal.LiveAggregates);
    }

    [Fact]
    public void SystemAudio_StartedTwice_ThrowsInvalidOperationAndLeavesTheRunningStreamAlone()
    {
        // InvalidOperationException, deliberately NOT the native failure type: a double start
        // is a bug in the caller rather than a Mac that refused, so the orchestrator's
        // skip-and-carry-on filter must not swallow it.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();

        Assert.IsType<InvalidOperationException>(Record.Exception(capture.Start));

        Assert.Equal(1, hal.LiveIoProcs);
        Assert.Equal(1, hal.RunningIoProcs);
    }

    [Fact]
    public void SystemAudio_WhenTheAggregateRefusesToStart_LeavesNoIoProcAndNoPumpBehind()
    {
        // The tray retries a device that refused, so a registration or a pump thread left by
        // each attempt is one per attempt for the process lifetime. The tap and the aggregate
        // deliberately SURVIVE: they are the ctor's, not this call's, and the retry reuses
        // them.
        FakeCoreAudioHal hal = WithSpeakers();
        hal.StartIoError = new CoreAudioException("starting the IOProc", -66780);
        using var capture = new MacOSSystemAudioCapture(hal);

        Assert.IsAssignableFrom<ExternalException>(Record.Exception(capture.Start));

        Assert.Equal(0, hal.LiveIoProcs);
        Assert.False(capture.IsPumping, "a Start that was refused left its pump thread parked");
        Assert.Equal(1, hal.LiveTaps);
        Assert.Equal(1, hal.LiveAggregates);
    }
}
