namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Settings applied to a RUNNING meeting (issues #149, #153). Connection and device changes
/// bind at the next Start, but the per-device level-gate knobs are pushed to the live
/// pipelines so a sensitivity change takes effect mid-meeting with no Stop/Start.
///
/// This is the portable half of the shell's Settings command: the dialog is the platform's,
/// the publish + persist + re-tune is not. Extracting it is what gives the AppKit shell the
/// live re-tune for free.
/// </summary>
public class BridgeRuntimeSettingsTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    // Quiet enough that the least-sensitive gate (slider 0, threshold 0.2) stays shut on it,
    // loud enough that the most-sensitive one (slider 100, threshold 0.001) opens: RMS 0.031.
    // Loud() is 0.244, which is above the WHOLE slider range, so it could never model a gate
    // that is deliberately deaf.
    private static byte[] Quiet(int frames) => Fixtures.Pcm(1000, frames);

    private static BridgeSettings WithMicSensitivity(FakeRecorder recorder, int sensitivity)
    {
        BridgeSettings settings = RuntimeHarness.RecorderSettings(recorder);
        settings.Devices =
        [
            new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Alice", "Alice")
            {
                Gate = new GateSettings(sensitivity, HangoverMs: 60, PreRollMs: 0),
            },
        ];
        return settings;
    }

    [Fact]
    public async Task ApplySettings_WhileAMeetingRuns_RetunesTheLivePipelineWithoutARestart()
    {
        await using FakeRecorder recorder = await FakeRecorder.StartAsync();
        using var harness = new RuntimeHarness
        {
            RealMint = true,
            Settings = WithMicSensitivity(recorder, sensitivity: 0), // deaf to quiet speech
        };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // The guard: at this tuning the gate really is shut, so the assertion after the
        // re-tune is about the re-tune rather than about audio that was always flowing.
        mic.Emit(Quiet(40));
        Assert.Equal(0, recorder.FramesFor(harness.SessionIdInUse!, "Alice"));

        runtime.ApplySettings(WithMicSensitivity(recorder, sensitivity: 100));

        // Same meeting, same pipeline, same open session: only the tuning changed.
        mic.Emit(Quiet(40));
        await Poll.UntilAsync(
            () => recorder.FramesFor(harness.SessionIdInUse!, "Alice") > 0,
            Wait, "the re-tuned gate to pass audio");
        Assert.True(harness.View.CanEnd, "the meeting was restarted rather than re-tuned");
        Assert.Equal(1, recorder.NewSessionCount);

        // Close the meeting rather than leaving it streaming into a Recorder that is about to
        // go away: an abandoned tap holds its drain budget open at teardown, which is a slow
        // test rather than a failing one and so is easy to leave behind.
        runtime.End();
        await RuntimeHarness.EndSettledAsync(runtime);
    }

    [Fact]
    public void ApplySettings_WhenTheSaveFails_KeepsTheEditForThisSessionAndSaysItWontPersist()
    {
        using var harness = new RuntimeHarness();
        harness.SettingsStoreDirectory = harness.UnwritableDirectory();
        BridgeRuntime runtime = harness.Build();

        BridgeSettings updated = new() { Host = "recorder.example", Port = 9999, Devices = [] };
        runtime.ApplySettings(updated);

        // The edit still governs this session: a disk that cannot be written is not a reason
        // to throw the operator's change away, and the live re-tune below it must still run.
        Assert.Equal("recorder.example", runtime.Settings.Host);

        // ...but they are told it will not survive a restart, rather than silently losing it.
        (string title, _, NoticeKind kind) = Assert.Single(harness.View.Notices);
        Assert.Equal("Settings not saved", title);
        Assert.Equal(NoticeKind.Warning, kind);
    }

    [Fact]
    public void ApplySettings_WhenTheSaveFailsWithABug_LetsItOutRatherThanBlamingTheDisk()
    {
        // The notice above is for what an operator can act on: a disk that will not take the file,
        // or a secret store that refuses. Anything else is this program being wrong, and reporting
        // a NullReferenceException as "Settings not saved" tells them to check their permissions
        // over a bug in here. CaptureSeam states the same rule for the device seam.
        using var harness = new RuntimeHarness { Tokens = new BuggyTapTokenStore() };
        BridgeRuntime runtime = harness.Build();

        Assert.Throws<InvalidOperationException>(
            () => runtime.ApplySettings(new BridgeSettings { Host = "recorder.example", Devices = [] }));
        Assert.Empty(harness.View.Notices);
    }

    /// <summary>A token store that fails the way a BUG does, which no temp directory can produce.
    /// </summary>
    private sealed class BuggyTapTokenStore : ITapTokenStore
    {
        public string? Write(string token) => throw new InvalidOperationException("a bug, not a disk");

        public string Read(string? atRest) => "";
    }
}
