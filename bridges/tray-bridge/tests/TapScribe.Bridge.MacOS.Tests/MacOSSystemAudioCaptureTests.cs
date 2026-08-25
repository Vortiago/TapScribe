using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS system-audio <see cref="IAudioCapture"/>: a Core Audio process tap inside a private
/// aggregate device (#420). Driven entirely through <see cref="FakeCoreAudioHal"/>, which validates
/// handle lifetime and refuses the orderings CoreAudio refuses, so the three objects' composition
/// and teardown are exercised on a lane with no audio hardware and no TCC grant.
///
/// This is the half of a meeting the Bridge exists for: the microphone is one speaker, and what the
/// Mac PLAYS is everyone else.
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
    public async Task ADefaultOutputMove_WhileTheAggregateIsLeaving_TearsDownRatherThanWedging()
    {
        // Both notifications this capture subscribes arrive on CoreAudio threads, and unplugging a
        // USB DAC that is BOTH the default output and the aggregate's sub-device fires both at
        // once. Removing a listener waits for a callback already inside it, so a rebind that
        // detaches the aggregate's listener while holding the binding lock waits for a handler
        // that is itself waiting for that lock, and the tray never ends the meeting.
        FakeCoreAudioHal hal = WithSpeakers();
        // Deliberately not a `using`: Dispose takes the binding lock, so a wedged capture would
        // wedge the test method too, and a hang is a worse failure report than a red assertion.
        // The success path disposes at the end.
        var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();
        uint aggregate = capture.AggregateDeviceId;

        using var rebindHoldsTheLock = new ManualResetEventSlim();
        using var goneIsBlocked = new ManualResetEventSlim();
        // ListDevices is the rebind's first call under the lock, so this runs with it held.
        hal.BeforeListDevices = () =>
        {
            if (rebindHoldsTheLock.IsSet)
                return;
            rebindHoldsTheLock.Set();
            goneIsBlocked.Wait(Wait);
        };

        Task rebind = Task.Run(() => hal.SetDefaultOutput(Devices.Output(71, "External Headphones")));
        Assert.True(rebindHoldsTheLock.Wait(Wait), "the rebind never reached the lock");

        Task gone = Task.Run(() => hal.FireProperty(aggregate, CoreAudioPropertyKind.DeviceIsAlive));
        // The handler's first statement takes the binding lock the rebind is holding, so once it
        // has been dispatched there is nothing else for it to be doing.
        await Task.Delay(100);
        goneIsBlocked.Set();

        Task both = Task.WhenAll(rebind, gone);
        Assert.True(
            await Task.WhenAny(both, Task.Delay(Wait)) == both,
            "the capture wedged: the rebind is waiting for a notification that is waiting for it");
        capture.Dispose();
    }

    [Fact]
    public void SystemAudio_WhileStarted_HoldsOneTapOneAggregateAndOneRunningIoProc()
    {
        // The whole shape of the platform in one assertion: a tap is an object with no audio path, an
        // aggregate device is what gives it an AudioObjectID, and only then is there something an
        // IOProc can run over. Counted because any one left unmade is a capture that delivers
        // nothing.
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
        // Three native objects and a listener, released in an order CoreAudio enforces: the IOProc
        // before the aggregate that carries it, the aggregate before the tap it lists. The fake
        // refuses each backwards, so reaching zero is the ORDER as much as the count. A tap left
        // behind is a private aggregate sitting in the operator's Mac until they log out.
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
        // "System audio" means what the Mac is PLAYING, which goes to the default output by
        // definition. An aggregate built around any other endpoint records silence, so this capture
        // finds the default itself rather than using whichever render device it was handed.
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
        // ExternalException is the capture seam's declared native failure, which is what BridgeRuntime
        // filters on to skip a device and still get the microphone into the session.
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
        // What a missing "System Audio Recording" grant looks like from here, and what a Mac below
        // the 14.4 floor would look like if one got this far. Same declared type: the caller's
        // question is "did the platform refuse", not "at which call".
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
        // The one ctor step that can fail with something already owned. A tap with no aggregate is
        // invisible to every counter anyone can read, and survives for the process lifetime.
        FakeCoreAudioHal hal = WithSpeakers();
        hal.CreateAggregateDeviceError = new CoreAudioException("creating the aggregate device", -66748);

        Assert.IsAssignableFrom<ExternalException>(
            Record.Exception(() => new MacOSSystemAudioCapture(hal)));

        Assert.Equal(0, hal.LiveTaps);
    }

    [Fact]
    public void SystemAudio_ReportsTheTapsOwnFormat_NotTheOutputDevices()
    {
        // The tap is a stereo mixdown CoreAudio resamples for us, and its format is a property of the
        // TAP rather than of the endpoint underneath. Reading the wrong one resamples 48 kHz stereo
        // float as whatever the speakers happen to be configured for.
        FakeCoreAudioHal hal = WithSpeakers();
        hal.TapFormat = Formats.Float32Stereo48k;
        using var capture = new MacOSSystemAudioCapture(hal);

        Assert.Equal(new AudioFormat(48_000, 2, SampleKind.Float32), capture.Format);
    }

    [Fact]
    public void SystemAudio_ReportsUnmuted_BecauseATapHasNoOsMuteToHonour()
    {
        // Matching the Windows loopback sibling: a render path has no mute event, so the level gate
        // is the only mute there is (#159). Asserted rather than left implicit, because "muted" would
        // hard-close the gate and record the far side of every meeting as silence.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);

        Assert.False(capture.IsMuted);
        // And it watches no mute property, so there is nothing that could ever flip it.
        Assert.Equal(0, hal.ListenerCount(CoreAudioObject.System, CoreAudioPropertyKind.Mute));
    }

    [Fact]
    public void SystemAudio_WhileStarted_SurfacesTapAudioAsDataAvailable()
    {
        // The point of the whole file. The fake refuses to deliver into a device with no RUNNING
        // IOProc, so this also pins that Start ran one on the AGGREGATE, not on the tap.
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
        // The other half of the release. A capture whose IOProc outlived its owner is a meeting still
        // streaming PCM after the operator ended it, and the fake refuses to model one.
        FakeCoreAudioHal hal = WithSpeakers();
        uint aggregate;
        // Scoped rather than method-scoped, because disposing IS the act here: a bare `using var`
        // would release after the push had already been attempted against a live handle.
        {
            using var capture = new MacOSSystemAudioCapture(hal);
            capture.Start();
            aggregate = capture.AggregateDeviceId;
        }

        Assert.Throws<InvalidOperationException>(() => hal.PushAudio(aggregate, [1, 2, 3, 4]));
    }

    [Fact]
    public void SystemAudio_OnACleanStop_RaisesFailedWithNoError()
    {
        // Failed is how the pipeline learns this capture stopped delivering, and the seam spells a
        // clean stop as a null payload precisely so it does not read as "system audio lost". Same
        // contract as the microphone's, because the pipeline cannot tell the two backends apart.
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
    public void SystemAudio_WhenTheAggregateGoesAwayMidStream_RaisesFailedWithTheReason()
    {
        // An aggregate whose sub-device leaves is itself invalidated, and CoreAudio stops calling the
        // IOProc. Without this the far side records as silence for the rest of the call with the
        // status line still claiming both devices are streaming.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);
        capture.Start();

        hal.FireProperty(capture.AggregateDeviceId, CoreAudioPropertyKind.DeviceIsAlive);

        Assert.IsAssignableFrom<ExternalException>(Assert.Single(failures));
    }

    [Fact]
    public void SystemAudio_StartedTwice_ThrowsInvalidOperationAndLeavesTheRunningStreamAlone()
    {
        // InvalidOperationException, deliberately NOT the native failure type: a double start is a
        // caller bug, so the orchestrator's skip-and-carry-on filter must not swallow it.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();

        Assert.IsType<InvalidOperationException>(Record.Exception(capture.Start));

        Assert.Equal(1, hal.LiveIoProcs);
        Assert.Equal(1, hal.RunningIoProcs);
    }

    [Fact]
    public void SystemAudio_WhenTheDefaultOutputMoves_RebindsTheTapToTheNewEndpoint()
    {
        // Plugging in headphones mid-meeting is the everyday case, and it moves what the Mac plays
        // through. The aggregate is built AROUND one endpoint, so left alone it records the rest of
        // the call as silence with the meeting still showing as streaming.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();

        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));

        Assert.Equal(["External Headphones:uid"], hal.LiveAggregateOutputs);
        // And exactly one of each, so the rebind REPLACED the binding rather than stacking a
        // second tap on top of a private aggregate device nobody will ever destroy.
        Assert.Equal(1, hal.LiveTaps);
        Assert.Equal(1, hal.LiveAggregates);
    }

    [Fact]
    public void SystemAudio_RebindingWhileStreaming_KeepsDeliveringFromTheNewEndpoint()
    {
        // The rebind is only worth doing if the meeting carries on through it. The fake refuses to
        // deliver into anything but a RUNNING IOProc on that exact device, so audio arriving from
        // the new aggregate is the whole claim.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<byte[]> received = [];
        using var arrived = new CountdownEvent(1);
        capture.DataAvailable += (_, e) =>
        {
            received.Add(e.Data.ToArray());
            arrived.Signal();
        };
        capture.Start();

        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));
        hal.PushAudio(capture.AggregateDeviceId, [7, 7, 7, 7]);

        Assert.True(arrived.Wait(Wait), "nothing arrived from the endpoint the tap moved to");
        Assert.Equal([[7, 7, 7, 7]], received);
        Assert.Equal(1, hal.RunningIoProcs);
    }

    [Fact]
    public void SystemAudio_RebindingWhileStopped_DoesNotStartAStreamNobodyAskedFor()
    {
        // A capture that was opened but never started still has to follow the output, because
        // its Format was read from the tap and Start must find a binding that matches. What it
        // must NOT do is come up streaming: the IOProc is Start's, and a rebind is not a Start.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);

        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));

        Assert.Equal(["External Headphones:uid"], hal.LiveAggregateOutputs);
        Assert.Equal(0, hal.LiveIoProcs);
        Assert.Equal(0, hal.RunningIoProcs);
    }

    [Fact]
    public void SystemAudio_WhenThePropertyFiresWithoutTheOutputMoving_KeepsTheBindingItHas()
    {
        // CoreAudio fires the default-output property on changes this capture has no stake in, and a
        // rebind destroys and rebuilds a tap and an aggregate device, dropping whatever lands in the
        // gap. Rebuilding on every notification would punch a hole in the recording each time.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();
        uint aggregate = capture.AggregateDeviceId;

        hal.FireProperty(CoreAudioObject.System, CoreAudioPropertyKind.DefaultOutputDevice);

        Assert.Equal(aggregate, capture.AggregateDeviceId);
        Assert.Equal(1, hal.LiveTaps);
        Assert.Equal(1, hal.LiveAggregates);
    }

    [Fact]
    public void SystemAudio_WhenTheNewEndpointsTapReadsDifferently_ReportsFailedRatherThanGarbage()
    {
        // Format is read once, at Open, and the Resampler downstream was built from it and cannot be
        // told otherwise mid-stream. An endpoint whose tap reads differently would have the bytes
        // reinterpreted at the wrong rate and channel count, which is noise recorded as speech.
        // Failed is what the pipeline surfaces as "system audio stopped".
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);
        capture.Start();

        hal.TapFormat = new CoreAudioStreamFormat(
            SampleRate: 44_100,
            ChannelsPerFrame: 2,
            BitsPerChannel: 32,
            FormatId: CoreAudioFormatId.LinearPcm,
            FormatFlags: CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked);
        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));

        Assert.NotNull(Assert.Single(failures));
        // Left unbound rather than half-bound: nothing is streaming, and the handles the
        // refused rebind made are gone rather than sitting in the operator's Mac.
        Assert.Equal(0, hal.RunningIoProcs);
        Assert.Equal(0, hal.LiveTaps);
        Assert.Equal(0, hal.LiveAggregates);
    }

    [Fact]
    public void SystemAudio_WhenTheRebindItselfIsRefused_ReportsFailedAndHoldsNothing()
    {
        // The output moved to an endpoint this Mac refuses to tap. Nothing is left to record the far
        // side with, so the pipeline is told rather than left with a capture that delivers nothing.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);
        capture.Start();

        hal.CreateAggregateDeviceError = new CoreAudioException("creating the aggregate device", -66748);
        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));

        Assert.IsAssignableFrom<ExternalException>(Assert.Single(failures));
        Assert.Equal(0, hal.LiveTaps);
        Assert.Equal(0, hal.LiveAggregates);
    }

    [Fact]
    public void SystemAudio_WhenTheRebuiltTapWillNotSayWhatItCarries_HoldsNothing()
    {
        // The one rebind failure that happens AFTER both system-wide objects exist: the tap and its
        // aggregate were built, and reading the tap's format refused. Neither has an owner at that
        // moment, so an unwind that releases only what it can SEE strands a process tap and a
        // Mac-wide aggregate for the life of the process, once per output switch.
        FakeCoreAudioHal hal = WithSpeakers();
        using var capture = new MacOSSystemAudioCapture(hal);
        List<Exception?> failures = [];
        capture.Failed += (_, e) => failures.Add(e);
        capture.Start();

        hal.ReadTapFormatError = new CoreAudioException("reading the format of the tap", -66748);
        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));

        Assert.IsAssignableFrom<ExternalException>(Assert.Single(failures));
        Assert.Equal(0, hal.RunningIoProcs);
        Assert.Equal(0, hal.LiveTaps);
        Assert.Equal(0, hal.LiveAggregates);
    }

    [Fact]
    public void SystemAudio_DisposedAfterARefusedRebind_StillReleasesCleanly()
    {
        // A capture that lost its binding is still an object its owner will Dispose, from a finally
        // with nothing to fall back on. Throwing would strand the rest of the teardown.
        FakeCoreAudioHal hal = WithSpeakers();
        var capture = new MacOSSystemAudioCapture(hal);
        capture.Start();
        hal.CreateProcessTapError = new CoreAudioException("creating a system-audio process tap", -66748);
        hal.SetDefaultOutput(Devices.Output(71, "External Headphones"));

        Assert.Null(Record.Exception(capture.Dispose));
        Assert.Equal(0, hal.LiveListeners);
    }

    [Fact]
    public void SystemAudio_WhenTheAggregateRefusesToStart_LeavesNoIoProcAndNoPumpBehind()
    {
        // The tray retries a device that refused, so a registration or a pump thread left behind is
        // one per attempt for the process lifetime. The tap and the aggregate deliberately SURVIVE:
        // they are the ctor's, not this call's, and the retry reuses them.
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
