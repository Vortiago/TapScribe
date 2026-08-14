using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// End meeting (issue #107) as Core behaviour: close every open tap, drain it to completion,
/// and only then trigger the Recorder's end-of-meeting pipeline, polling it to the summary.
///
/// Driven against a live <see cref="FakeRecorder"/> over real sockets, because the ordering is
/// the whole point and a faked control client would assert nothing about it: a WAV that is
/// still being written when the pipeline strips it is the bug this bracket exists to prevent.
/// </summary>
public class BridgeRuntimeEndTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    [Fact]
    public async Task End_AfterAMeeting_DrainsEveryTapBeforeTriggeringThePipeline()
    {
        await using FakeRecorder recorder = await FakeRecorder.StartAsync();
        using var harness = new RuntimeHarness
        {
            Recorder = recorder,
            Settings = RuntimeHarness.RecorderSettings(recorder),
        };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        FakeAudioCapture system = harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // Real audio through both taps, so there is something to drain rather than an empty
        // bracket that would pass whatever the ordering. The identities are the DEFAULT pair's
        // labels, not the device ids: with no saved selection a meeting taps the mic under the
        // operator's display name and the loopback under "System audio", which is exactly the
        // attribution the Recorder buckets WAVs by.
        mic.Emit(Loud(40));
        system.Emit(Loud(40));
        await Poll.UntilAsync(
            () => recorder.FramesFor(harness.SessionIdInUse!, "Alice") > 0
                && recorder.FramesFor(harness.SessionIdInUse!, "System audio") > 0,
            Wait, "both taps to reach the Recorder");

        runtime.End();
        await RuntimeHarness.EndSettledAsync(runtime);

        // The causal claim, not a timing one: every tap closed normally, and the pipeline ran.
        // The Recorder records both, so "did the drain finish first" is answerable without a
        // clock anywhere in the assertion.
        Assert.True(recorder.AllTapsClosed(harness.SessionIdInUse!), "a tap was still open when End returned");
        Assert.Equal(1, recorder.TriggerCount(harness.SessionIdInUse!));

        // Ending releases the devices: dropping this leaks the endpoints and lets them keep
        // streaming PCM into the session past the barrier.
        Assert.True(mic.Disposed, "the mic was left open after the meeting ended");
        Assert.True(system.Disposed, "the system capture was left open after the meeting ended");
        Assert.True(harness.Enumerator.Disposed, "the device enumerator outlived the meeting");
    }

    /// <summary>Record-only: End drains and closes the taps but fires no pipeline, so the
    /// teardown is exercised with no Recorder in the picture at all.</summary>
    private static BridgeSettings RecordOnly()
    {
        BridgeSettings settings = RuntimeHarness.DefaultSettings();
        settings.ProcessOnEnd = false;
        return settings;
    }

    [Fact]
    public async Task End_RecordOnly_ReleasesTheDevices_AndReturnsToIdle()
    {
        // The guardrail for the test below: the healthy record-only End lands on released
        // devices and a usable menu, so "returns the commands" cannot pass by never taking them
        // away, and "releases the devices" cannot pass by a teardown that never runs.
        using var harness = new RuntimeHarness { Settings = RecordOnly() };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        harness.AddDevice("system", DeviceFlow.Render);
        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        runtime.End();
        await RuntimeHarness.EndSettledAsync(runtime);

        Assert.True(mic.Stopped && mic.Disposed, "End must stop and release the capture");
        Assert.True(harness.Enumerator.Disposed);
        Assert.True(harness.Enumerator.CapturesReleasedFirst,
            "the enumerator was released while a capture it opened was still live");
        Assert.True(harness.View.CanStart);
        Assert.False(harness.View.CanEnd);
        Assert.Contains(harness.View.Notices, n => n.Title == "Recording saved");
    }

    [Fact]
    public async Task End_WhenTheDrainThrowsUnexpectedly_ReturnsTheCommandsToTheOperator()
    {
        // A failure OUTSIDE the flow's catch filter escapes a fire-and-forget task that nobody
        // observes, and both commands are disabled with the header stuck on "Ending meeting…":
        // a tray that has to be restarted. The exception is still not this flow's to classify,
        // so it propagates; what the flow owes the operator is a usable menu on the way out.
        using var harness = new RuntimeHarness { Settings = RecordOnly() };
        harness.AddDevice(
            "mic", DeviceFlow.Capture,
            new FakeAudioCapture(RecorderFormat) { DetachError = new IOException("endpoint invalidated") });
        harness.AddDevice("system", DeviceFlow.Render);
        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        runtime.End();
        // The escape is the anti-vacuity guard: the flow really took the path outside its catch
        // filter, which is the one that used to leave the menu stranded. The busy state in
        // between is not observable here, and that is the harness rather than the runtime: the
        // drain faults without yielding and the tests' dispatcher runs the recovery inline, so
        // both have happened by the time End returns.
        await Assert.ThrowsAsync<IOException>(() => RuntimeHarness.EndSettledAsync(runtime));

        Assert.True(harness.View.CanStart,
            "both commands were left disabled: the shell is unusable until it is restarted");
        Assert.False(harness.View.CanEnd);
        Assert.Equal(TrayIcon.Error, harness.View.LastStatus!.Icon);
        Assert.DoesNotContain("Ending meeting", harness.View.LastStatus.Header, StringComparison.Ordinal);
    }

    [Fact]
    public void End_WithNoMeetingRunning_IsANoOp()
    {
        using var harness = new RuntimeHarness();
        BridgeRuntime runtime = harness.Build();

        runtime.End();

        // Nothing to end, so nothing changes: the menu must not go busy and strand the
        // operator with both commands disabled and no flow to re-enable them.
        Assert.Null(runtime.EndTask);
        Assert.True(harness.View.CanStart);
        Assert.False(harness.View.CanEnd);
    }
}
