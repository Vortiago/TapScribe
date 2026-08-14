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
