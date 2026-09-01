namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Connect / Disconnect: an attached tap streams into the Recorder's CURRENT session instead
/// of a session this bridge minted (ADR-0025). What a microphone in a meeting room wants, with
/// nobody at the keyboard.
///
/// The claims worth holding are all about what the mode does NOT do — it does not mint, it does
/// not name a session, and disconnecting does not trigger a pipeline — so most of these
/// assertions are about things that did not happen. Each one names why it would be wrong if it
/// did.
/// </summary>
public class BridgeRuntimeConnectTests
{
    [Fact]
    public async Task Connect_WithTwoResolvableDevices_StreamsWithoutMintingASession()
    {
        using var harness = new RuntimeHarness();
        harness.AddDevice("mic", DeviceFlow.Capture);
        harness.AddDevice("system", DeviceFlow.Render);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);

        // The header says CONNECTED TO LIVE, not Streaming: an operator who read "Streaming"
        // here would look for an End that is deliberately not offered. Zero connected is
        // expected and not the subject, as in the Start tests — nothing is listening on the
        // harness's port.
        Assert.Equal(StatusView.For(new TrayStatus.Attached(0, 2)), harness.View.LastStatus);

        // Not one round-trip to /api/tap/new-session. This is the mode's whole point: the
        // Recorder decides which session the audio lands in, and a bridge that minted one would
        // be recording the room into a session nobody is looking at.
        Assert.False(harness.MintReached.IsCompleted, "connecting minted a detached session");

        // Disconnect is the way out; Start stays live because from here it is a TAKEOVER.
        Assert.Equal(TrayCommands.Attached, harness.View.Commands);
    }

    [Fact]
    public async Task Connect_WhenThePreflightRefusesTheToken_ReturnsToIdleWithoutOpeningADevice()
    {
        // The pre-flight exists for exactly this: Start gets one free because its mint is a
        // round-trip, and an attached tap has no mint. Without it a refused token is silent
        // until the first person speaks — the taps open lazily, on speech — and by then nobody
        // is watching the tray.
        using var harness = new RuntimeHarness
        {
            PreflightResult = new ConnectionTestResult(
                Reachable: true, ReachError: null,
                TokenChecked: true, TokenAccepted: false, TokenError: "401 unauthorized"),
        };
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);

        Assert.Equal(TrayCommands.Idle, harness.View.Commands);
        Assert.Contains(harness.View.Notices, n => n.Title == "Could not connect");
        Assert.False(mic.Started, "a device was opened despite the pre-flight refusing the token");
    }

    [Fact]
    public async Task Connect_WhenNoSelectedDeviceIsPresent_ReturnsToIdleWithoutTheRoundTrip()
    {
        // The selection verdict is a hard stop BEFORE any network call, the same order Start
        // keeps: there is nothing to ask the Recorder about if there is nothing to record with.
        using var harness = new RuntimeHarness(); // no devices registered at all

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);

        Assert.False(harness.PreflightReached.IsCompleted, "the Recorder was probed with nothing to record");
        Assert.Equal(TrayCommands.Idle, harness.View.Commands);
        Assert.Contains(harness.View.Notices, n => n.Title == "Could not connect");
    }

    [Fact]
    public async Task Connect_WhileAlreadyAttached_IsRefused()
    {
        // There is no takeover in this direction: connecting twice is not something an operator
        // can mean, and a second set of taps under the same identity would split one speaker.
        using var harness = new RuntimeHarness();
        harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);
        Task? first = runtime.ConnectTask;

        runtime.Connect();

        Assert.Same(first, runtime.ConnectTask);
        Assert.Equal(TrayCommands.Attached, harness.View.Commands);
    }

    [Fact]
    public async Task Disconnect_DrainsTheTapsAndReturnsToIdle_WithoutTriggeringAPipeline()
    {
        using var harness = new RuntimeHarness();
        FakeAudioCapture mic = harness.AddDevice("mic", DeviceFlow.Capture);

        BridgeRuntime runtime = harness.Build();
        runtime.Connect();
        await RuntimeHarness.ConnectSettledAsync(runtime);

        runtime.Disconnect();
        await RuntimeHarness.DisconnectSettledAsync(runtime);

        // Drained and released: the last Utterance's WAV is flushed, so the Recorder is not
        // handed a truncated file to strip and transcribe.
        Assert.True(mic.Disposed, "the attached taps were not released");
        Assert.Equal(TrayCommands.Idle, harness.View.Commands);
        Assert.Contains(harness.View.Notices, n => n.Title == "Disconnected");

        // No pipeline flow ever existed. An attached tap has no session id, and the trigger,
        // the poll, the Past-meetings entry and the restart-resume state are every one of them
        // keyed on one — so this is enforced by the types, and this assertion is what says so.
        Assert.Null(runtime.EndTask);
        Assert.Empty(harness.View.Windows);
    }

    [Fact]
    public void Disconnect_WithNothingAttached_DoesNothing()
    {
        using var harness = new RuntimeHarness();
        BridgeRuntime runtime = harness.Build();

        runtime.Disconnect();

        Assert.Null(runtime.DisconnectTask);
        Assert.Equal(TrayCommands.Idle, harness.View.Commands);
    }
}
