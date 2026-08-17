using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS <see cref="IAudioDeviceEnumerator"/>: which endpoints the bridge is willing to
/// tap, and how one is opened (#419). Driven through <see cref="FakeCoreAudioHal"/>, so the
/// mapping and the refusals are exercised on a lane with no audio devices at all, which is
/// the case a real enumerator can never be asked about.
/// </summary>
public class MacOSAudioDeviceEnumeratorTests
{
    [Fact]
    public void List_ReportsTheInputDevicesWithTheirNames()
    {
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        hal.AddDevice(Devices.Input(57, "Yeti Nano"));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        IReadOnlyList<CaptureDevice> devices = enumerator.List();

        Assert.Equal(["Built-in Microphone", "Yeti Nano"], devices.Select(d => d.Name));
        Assert.All(devices, d => Assert.Equal(DeviceFlow.Capture, d.Flow));
    }

    [Fact]
    public void List_KeysEachDeviceOnItsPersistentUid_NotItsObjectId()
    {
        // CaptureDevice.Id is what a saved device selection round-trips through, and CoreAudio
        // re-issues AudioObjectIDs per boot and per replug: keyed on one, a saved mic names a
        // different device next launch, or nothing. The UID is the stable half, so it is the
        // id, and Open resolves it back to whatever object id the device has now.
        var hal = new FakeCoreAudioHal();
        CoreAudioDevice device = hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        CaptureDevice listed = Assert.Single(enumerator.List());

        Assert.Equal(device.Uid, listed.Id);
        Assert.NotEqual(device.ObjectId.ToString(System.Globalization.CultureInfo.InvariantCulture), listed.Id);
    }

    [Fact]
    public void List_FlagsTheSystemDefaultInput_AndOffersNoOutputDevice()
    {
        // Two claims that only mean something together. The HAL reports every device scope,
        // including outputs, each carrying its OWN default flag; a default output reaching the
        // list would put an endpoint in the picker that this backend cannot open, because
        // capturing system audio on macOS is a process tap rather than a device (#420) and is
        // not this slice. Filtering to inputs is what leaves exactly one flagged default for
        // the follow-default rule to find.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        hal.AddDevice(Devices.Input(57, "Yeti Nano", isDefault: true));
        hal.AddDevice(Devices.Output(63, "MacBook Pro Speakers", isDefault: true));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        IReadOnlyList<CaptureDevice> devices = enumerator.List();

        Assert.Equal(["Built-in Microphone", "Yeti Nano"], devices.Select(d => d.Name));
        Assert.Equal("Yeti Nano", CaptureDevice.DefaultFor(devices, DeviceFlow.Capture)?.Name);
    }
}
