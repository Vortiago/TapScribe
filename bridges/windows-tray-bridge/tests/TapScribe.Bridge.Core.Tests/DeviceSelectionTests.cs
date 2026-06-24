using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="DeviceSelection"/> resolution — the pure
/// (saved selections + currently-available devices) -> (resolved, missing, verdict)
/// seam the tray shell calls at Start meeting. No WASAPI, no WinForms; this is the
/// device logic that runs on the cross-platform CI runner (ADR-0005).
/// </summary>
public class DeviceSelectionTests
{
    private static CaptureDevice Mic(string id, bool isDefault = false) =>
        new(id, $"Mic {id}", DeviceFlow.Capture, isDefault);

    private static CaptureDevice Speakers(string id, bool isDefault = false) =>
        new(id, $"Speakers {id}", DeviceFlow.Render, isDefault);

    [Fact]
    public void Resolve_FollowDefault_BindsToTheCurrentDefaultOfThatFlow()
    {
        // Two mics present; the saved selection is "follow the default mic", not a
        // pinned id, so it must bind to whichever mic is default RIGHT NOW.
        var nonDefault = Mic("usb", isDefault: false);
        var current = Mic("builtin", isDefault: true);
        IReadOnlyList<CaptureDevice> available = [nonDefault, current];

        ResolveResult result = DeviceSelection.Resolve(
            [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "My Mic")],
            available);

        ResolvedDevice resolved = Assert.Single(result.Resolved);
        Assert.Equal("builtin", resolved.Device.Id); // the default, not the USB mic
        Assert.Equal("mic", resolved.Identity);
        Assert.Equal("My Mic", resolved.Name);
        Assert.Empty(result.Missing);
    }

    [Fact]
    public void Resolve_PinnedDevicePresent_BindsToThatExactDevice()
    {
        // A power user pinned a specific USB interface, NOT the default.
        var usb = Mic("usb", isDefault: false);
        var builtin = Mic("builtin", isDefault: true);
        IReadOnlyList<CaptureDevice> available = [usb, builtin];

        ResolveResult result = DeviceSelection.Resolve(
            [new DeviceSelection.Pinned("usb", "mic", "USB Mic")],
            available);

        ResolvedDevice resolved = Assert.Single(result.Resolved);
        Assert.Equal("usb", resolved.Device.Id); // the pinned one, even though it isn't default
        Assert.Empty(result.Missing);
    }

    [Fact]
    public void Resolve_PinnedDeviceAbsent_ReportsItMissing_NotResolved()
    {
        // The pinned USB mic was unplugged: it must surface as missing (a non-fatal
        // warning at Start), never silently resolve to some other device.
        IReadOnlyList<CaptureDevice> available = [Mic("builtin", isDefault: true)];
        var gone = new DeviceSelection.Pinned("usb", "mic", "USB Mic");

        ResolveResult result = DeviceSelection.Resolve([gone], available);

        Assert.Empty(result.Resolved);
        Assert.Same(gone, Assert.Single(result.Missing));
    }

    [Fact]
    public void Resolve_WhenAtLeastOneDeviceResolves_VerdictIsOk()
    {
        ResolveResult result = DeviceSelection.Resolve(
            [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic")],
            [Mic("builtin", isDefault: true)]);

        Assert.Equal(SelectionVerdict.Ok, result.Verdict);
    }

    [Fact]
    public void Resolve_WhenNothingResolves_VerdictIsNothingToCapture()
    {
        // No mic present at all (and a pinned loopback that's also gone): zero devices
        // to open. Start must abort with a clear message, not open an empty meeting.
        IReadOnlyList<CaptureDevice> available = [];

        ResolveResult result = DeviceSelection.Resolve(
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic"),
                new DeviceSelection.Pinned("gone", "system", "System"),
            ],
            available);

        Assert.Empty(result.Resolved);
        Assert.Equal(SelectionVerdict.NothingToCapture, result.Verdict);
    }

    [Fact]
    public void Resolve_TwoResolvedDevicesSharingIdentity_VerdictIsDuplicateIdentity()
    {
        // Both selections resolve (default mic + pinned USB) but stream under the SAME
        // identity "mic" — the Recorder buckets WAVs by identity, so this would
        // cross-attribute. Catch it as a verdict BEFORE opening devices, rather than
        // letting CaptureOrchestrator.StartAll throw mid-open.
        IReadOnlyList<CaptureDevice> available = [Mic("builtin", isDefault: true), Mic("usb")];

        ResolveResult result = DeviceSelection.Resolve(
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "A"),
                new DeviceSelection.Pinned("usb", "mic", "B"),
            ],
            available);

        Assert.Equal(SelectionVerdict.DuplicateIdentity, result.Verdict);
    }

    [Fact]
    public void ToTapOptions_StampsTheDetachedSessionAndPerDeviceIdentityName()
    {
        ResolveResult result = DeviceSelection.Resolve(
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "My Mic"),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "system", "System Audio"),
            ],
            [Mic("builtin", isDefault: true), Speakers("spk", isDefault: true)]);

        var baseConn = new TapConnectionOptions { Host = "rec", Port = 9000, Tls = true, Token = "tok" };
        IReadOnlyList<TapConnectionOptions> options =
            result.ToTapOptions("2026-06-17T10-00-00", baseConn);

        // Every device routes into the ONE detached session — the isolation property.
        Assert.Equal(2, options.Count);
        Assert.All(options, o => Assert.Equal("2026-06-17T10-00-00", o.Session));

        // Connection coordinates carry through from the base unchanged.
        Assert.All(options, o =>
        {
            Assert.Equal("rec", o.Host);
            Assert.Equal(9000, o.Port);
            Assert.True(o.Tls);
            Assert.Equal("tok", o.Token);
        });

        // Each device keeps its own identity + display name (the per-speaker split).
        TapConnectionOptions mic = Assert.Single(options, o => o.Identity == "mic");
        Assert.Equal("My Mic", mic.Name);
        TapConnectionOptions system = Assert.Single(options, o => o.Identity == "system");
        Assert.Equal("System Audio", system.Name);
    }

    [Fact]
    public void Resolve_FollowDefault_WhenNoDeviceIsDefault_BindsToFirstOfThatFlow()
    {
        // Active mics present but none marked default (headless / RDP / no default
        // endpoint configured): follow-default must still bind, not refuse.
        IReadOnlyList<CaptureDevice> available = [Mic("a"), Mic("b")];

        ResolveResult result = DeviceSelection.Resolve(
            [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic")],
            available);

        ResolvedDevice resolved = Assert.Single(result.Resolved);
        Assert.Equal("a", resolved.Device.Id);
        Assert.Equal(SelectionVerdict.Ok, result.Verdict);
    }

    [Fact]
    public void Resolve_CarriesEachSelectionsGate_ToTheResolvedDevice()
    {
        // Per-device tuning rides along the selection through resolution, so the tray can
        // build each pipeline's LevelGate from its own device's gate (ADR-0007).
        var micGate = new GateSettings(Sensitivity: 40, HangoverMs: 700, PreRollMs: 200);
        var systemGate = new GateSettings(Sensitivity: 80, HangoverMs: 900, PreRollMs: 350);

        ResolveResult result = DeviceSelection.Resolve(
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic", micGate),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "system", "System", systemGate),
            ],
            [Mic("builtin", isDefault: true), Speakers("spk", isDefault: true)]);

        Assert.Equal(micGate, Assert.Single(result.Resolved, r => r.Identity == "mic").Gate);
        Assert.Equal(systemGate, Assert.Single(result.Resolved, r => r.Identity == "system").Gate);
    }

    [Fact]
    public void Resolve_FillsTheFlowDefaultGate_WhenTheSelectionCarriesNone()
    {
        // A selection with no gate (a pre-per-device file, or a direct caller) must still
        // resolve to a concrete gate so the pipeline always has one — defaulted by the
        // RESOLVED device's flow, so a loopback gets the sensitive default even when the
        // selection was silent.
        ResolveResult result = DeviceSelection.Resolve(
            [
                new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "Mic"),
                new DeviceSelection.FollowDefault(DeviceFlow.Render, "system", "System"),
            ],
            [Mic("builtin", isDefault: true), Speakers("spk", isDefault: true)]);

        Assert.Equal(
            GateSettings.DefaultForFlow(DeviceFlow.Capture),
            Assert.Single(result.Resolved, r => r.Identity == "mic").Gate);
        Assert.Equal(
            GateSettings.DefaultForFlow(DeviceFlow.Render),
            Assert.Single(result.Resolved, r => r.Identity == "system").Gate);
    }

    [Fact]
    public void ToTapOptions_PropagatesAllowSelfSignedCert_FromTheBaseOptions()
    {
        // The insecure self-signed opt-in is a connection coordinate, so it must ride
        // through to every per-device tap the same way host/port/tls/token do (the record
        // `with` copy), or a meeting's taps would silently lose the operator's setting.
        ResolveResult result = DeviceSelection.Resolve(
            [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "mic", "My Mic")],
            [Mic("builtin", isDefault: true)]);

        var baseConn = new TapConnectionOptions { Tls = true, AllowSelfSignedCert = true };
        TapConnectionOptions opt = Assert.Single(result.ToTapOptions("sess", baseConn));

        Assert.True(opt.AllowSelfSignedCert);
    }

    [Fact]
    public void ToTapOptions_BlankIdentityAndName_FallBackToTheBaseIdentity()
    {
        ResolveResult result = DeviceSelection.Resolve(
            [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "", "")],
            [Mic("builtin", isDefault: true)]);

        var baseConn = new TapConnectionOptions { Identity = "fallback-id", Name = "base-name" };
        TapConnectionOptions opt = Assert.Single(result.ToTapOptions("sess", baseConn));

        Assert.Equal("fallback-id", opt.Identity); // blank Speaker ID -> base identity (never empty)
        Assert.Equal("fallback-id", opt.Name);     // blank display name -> the effective identity
    }
}
