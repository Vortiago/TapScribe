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
}
