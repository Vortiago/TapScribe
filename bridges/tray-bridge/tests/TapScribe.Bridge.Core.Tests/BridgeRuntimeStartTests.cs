using System.Net;
using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The meeting lifecycle, hoisted out of the WinForms shell into Core (issue #419, slice 0B).
/// Start resolves the operator's device selection against the devices present now, mints a
/// detached session, and runs one pipeline per resolved device: reporting everything the
/// operator would see through <see cref="ITrayView"/>.
///
/// Driven here with no WinForms, no WASAPI endpoint and no Recorder, which is the whole point
/// of the extraction: the AppKit shell inherits this behaviour with this cover, instead of
/// re-implementing an untestable copy of it.
/// </summary>
public class BridgeRuntimeStartTests
{
    [Fact]
    public async Task Start_WithTwoResolvableDevices_PublishesAStreamingMeeting()
    {
        using var harness = new RuntimeHarness();
        harness.AddDevice("mic", DeviceFlow.Capture);
        harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // The meeting is live: End is the only move left, and the header names both devices.
        // Zero CONNECTED is expected and is not the subject: the harness points at a port
        // nothing listens on, so the taps are streaming into a refused connection while the
        // core retries. What Start owns is that two pipelines exist and the meeting was
        // published; whether a tap reached the Recorder is TapStream's own cover.
        Assert.Equal(StatusView.For(new TrayStatus.Streaming(0, 2)), harness.View.LastStatus);
        Assert.False(harness.View.CanStart, "Start stayed enabled over a live meeting");
        Assert.True(harness.View.CanEnd, "the meeting was never published as running");
    }

    [Fact]
    public async Task Start_WhenNoSelectedDeviceIsPresent_ReturnsToIdleWithoutMintingASession()
    {
        using var harness = new RuntimeHarness(); // no devices registered at all

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // A non-Ok verdict is a hard stop surfaced BEFORE any network call: the operator's
        // devices are gone, and minting a session first would leave an empty detached session
        // on the Recorder for every failed start.
        Assert.False(harness.MintReached.IsCompleted, "the pre-flight verdict did not short-circuit the mint");

        // Back to idle, with a message that names the fix rather than the fault.
        Assert.True(harness.View.CanStart, "Start stayed greyed out after a failed start");
        Assert.False(harness.View.CanEnd);
        Assert.Equal(TrayIcon.Error, harness.View.LastStatus!.Icon);
        (string title, string message, NoticeKind kind) = Assert.Single(harness.View.Notices);
        Assert.Equal(NoticeKind.Warning, kind);
        Assert.Equal("Could not start meeting", title);
        Assert.Contains("Settings", message, StringComparison.Ordinal);

        // The enumerator is opened before the verdict is known, so this early exit is one of
        // the paths that has to release it.
        Assert.True(harness.Enumerator.Disposed, "the device enumerator was stranded");
    }

    [Fact]
    public async Task Start_WhenTheRecorderRefusesTheToken_SaysSoAndLeavesNoMeetingBehind()
    {
        using var harness = new RuntimeHarness
        {
            MintError = new HttpRequestException("Unauthorized", null, HttpStatusCode.Unauthorized),
        };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // Classified, not raw: "status code 401" tells the operator nothing about what to do.
        (string title, string message, _) = Assert.Single(harness.View.Notices);
        Assert.Equal("Could not start meeting", title);
        Assert.Equal(
            StartFailure.Classify(harness.MintError!, harness.Settings.Host, harness.Settings.Port).Message,
            message);

        // The mint runs BEFORE any device is opened, which is what makes it the pre-flight:
        // a refused token must not have cost the operator an opened endpoint.
        Assert.False(mic.Started, "a device was opened despite the pre-flight failing");
        Assert.True(harness.Enumerator.Disposed, "the device enumerator was stranded");
        Assert.True(harness.View.CanStart, "Start stayed greyed out after a failed start");
        Assert.False(harness.View.CanEnd);
    }

    [Fact]
    public async Task Start_WhenTheRecorderIsUnreachable_SaysSoRatherThanWedgingOnStarting()
    {
        using var harness = new RuntimeHarness { MintError = new HttpRequestException("refused") };
        harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // The distinction the operator acts on: unreachable means check the host/port, a
        // rejected token means check the token. Both must beat leaving the header on
        // "Starting…" forever, which is what an unclassified escape from this fire-and-forget
        // task would do.
        Assert.Equal(
            StartFailureKind.Unreachable,
            StartFailure.Classify(harness.MintError!, harness.Settings.Host, harness.Settings.Port).Kind);
        Assert.Equal(TrayIcon.Error, harness.View.LastStatus!.Icon);
        Assert.True(harness.View.CanStart);
    }

    [Fact]
    public async Task Start_WhenOneDeviceCannotBeOpened_RecordsTheMeetingOnTheRest()
    {
        using var harness = new RuntimeHarness();
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        harness.AddDevice("system", DeviceFlow.Render);
        // The endpoint the platform refuses: in use by an exclusive-mode client, or gone
        // between List and Open. ExternalException is the capture seam's declared native
        // failure, so this is the shape every backend raises.
        harness.Enumerator.FailOpen("system", new ExternalException("endpoint in use"));

        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);

        // A dead loopback must not stop the mic from recording: that is the whole reason
        // opening is best-effort per device rather than a set that succeeds or fails together.
        Assert.True(mic.Started, "the surviving device never started");
        Assert.True(harness.View.CanEnd, "the meeting was not published");
        Assert.Equal(StatusView.For(new TrayStatus.Streaming(0, 1)), harness.View.LastStatus);

        // ...and the operator is told which device dropped out, by name.
        (string title, _, NoticeKind kind) = Assert.Single(harness.View.Notices);
        Assert.Equal("Could not open system", title);
        Assert.Equal(NoticeKind.Warning, kind);
    }

    [Fact]
    public async Task Start_WhileAnotherStartIsInFlight_IsRefused()
    {
        // A meeting exists from the operator's first click, not from the moment it is
        // published, and the mint is a network round-trip long. With only the published meeting
        // as a guard, that whole span was unguarded in the runtime's own model and a greyed-out
        // menu item was the only thing between a second click and a second meeting.
        using var harness = new RuntimeHarness { HoldMint = true };
        harness.AddDevice("mic", DeviceFlow.Capture);
        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await harness.MintReached;
        Task first = runtime.StartTask!;

        runtime.Start(); // a second click, mid-mint

        Assert.Same(first, runtime.StartTask); // no second start was ever launched
        harness.CompleteMint();
        await RuntimeHarness.StartSettledAsync(runtime);
        await runtime.QuitAsync();
    }

    [Fact]
    public async Task Start_AfterQuit_IsRefused()
    {
        using var harness = new RuntimeHarness();
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);
        BridgeRuntime runtime = harness.Build();

        await runtime.QuitAsync();
        runtime.Start();

        Assert.Null(runtime.StartTask);
        Assert.False(mic.Started, "a meeting was started on a shell that is already gone");
    }

    [Fact]
    public async Task ADeviceThatKeepsDropping_NoticesOnce_AndKeepsSayingSoInTheStatus()
    {
        // A dropped device reports once per Utterance for the rest of the meeting. The status
        // line has to say so throughout, but the operator must be TOLD once: a notice is a real
        // 4-second toast on Windows, so the naive wiring toasts every utterance until the
        // meeting ends. The device is named by the IDENTITY its tap streams under, which is what
        // the Recorder attributes its recordings to, not by the endpoint's device name.
        const string micIdentity = "Alice";
        BridgeSettings settings = RuntimeHarness.DefaultSettings();
        settings.Devices =
        [
            new DeviceSelection.FollowDefault(DeviceFlow.Capture, micIdentity, micIdentity),
            new DeviceSelection.FollowDefault(DeviceFlow.Render, "System audio", "System audio"),
        ];
        using var harness = new RuntimeHarness { Settings = settings };
        FakeAudioCapture mic = harness.AddDevice("mic-endpoint", DeviceFlow.Capture);
        harness.AddDevice("system-endpoint", DeviceFlow.Render);
        BridgeRuntime runtime = harness.Build();
        runtime.Start();
        await RuntimeHarness.StartSettledAsync(runtime);
        int before = harness.View.Notices.Count;

        mic.RaiseFailed(new IOException("endpoint gone"));
        mic.RaiseFailed(new IOException("endpoint gone")); // the next utterance says the same
        mic.RaiseFailed(new IOException("endpoint gone")); // ...and the next

        (string title, _, _) = Assert.Single(harness.View.Notices.Skip(before));
        Assert.Equal($"{micIdentity} stopped", title);
        // The status still says it, though: that is what the header is for.
        Assert.Contains($"{micIdentity} stopped", harness.View.LastStatus!.Header, StringComparison.Ordinal);
        Assert.Equal(TrayIcon.Error, harness.View.LastStatus.Icon);

        await runtime.QuitAsync();
    }
}
