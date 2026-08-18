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

    [Fact]
    public void Open_ResolvesTheUidBackToWhateverObjectIdTheDeviceHasNow()
    {
        // The round trip List keyed on. The capture reads its format through the HAL, so the
        // format coming back is the proof it was opened against the RIGHT object id rather
        // than the first device on the list.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        hal.AddDevice(Devices.Input(57, "Yeti Nano"), new CoreAudioStreamFormat(
            SampleRate: 44_100,
            ChannelsPerFrame: 1,
            BitsPerChannel: 16,
            FormatId: CoreAudioFormatId.LinearPcm,
            FormatFlags: CoreAudioFormatFlags.IsSignedInteger | CoreAudioFormatFlags.IsPacked));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);
        CaptureDevice yeti = enumerator.List().Single(d => d.Name == "Yeti Nano");

        using IAudioCapture capture = enumerator.Open(yeti);

        Assert.Equal(new AudioFormat(44_100, 1, SampleKind.Int16), capture.Format);
    }

    // Both refusals are the seam's ArgumentException clause, "the id names no active endpoint
    // of the requested flow", because on this backend they are the same fact. An id that
    // vanished (the mic was unplugged since the picker was drawn, or a saved selection names a
    // device that is not here) and an id that names an output both come down to: nothing in
    // the list matches. ExternalException would be wrong for either, since the platform
    // answered fine, and the callers that skip a dead endpoint filter on that one.
    public static TheoryData<string, CaptureDevice> UnopenableDevices() => new()
    {
        {
            "a UID no listed device carries",
            new CaptureDevice("unplugged-since-the-picker-was-drawn:uid", "Ghost", DeviceFlow.Capture, false)
        },
        {
            "an output endpoint, which this backend does not capture",
            new CaptureDevice("MacBook Pro Speakers:uid", "MacBook Pro Speakers", DeviceFlow.Render, true)
        },
        {
            "a listed input asked for under the wrong flow",
            new CaptureDevice("Built-in Microphone:uid", "Built-in Microphone", DeviceFlow.Render, false)
        },
    };

    [Theory]
    [MemberData(nameof(UnopenableDevices))]
    public void Open_ADeviceThisBackendCannotHonour_ThrowsArgumentException(
        string why, CaptureDevice device)
    {
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        hal.AddDevice(Devices.Output(63, "MacBook Pro Speakers", isDefault: true));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        var thrown = Assert.Throws<ArgumentException>(() => enumerator.Open(device));

        Assert.Contains(device.Id, thrown.Message, StringComparison.Ordinal);
        Assert.False(string.IsNullOrWhiteSpace(why));
    }

    [Fact]
    public void Dispose_ReleasesTheHalOnce_AndNotTheCapturesItHandedOut()
    {
        // The seam's ownership rule: an enumerator hands its endpoint over to each capture it
        // opens and so must OUTLIVE them, which means releasing the captures is somebody
        // else's job. Counted rather than flagged, because an owner fixing a leak must not
        // turn it into a double release: disposal is bound to be throw-free, never idempotent.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"), mute: false);
        // Scoped rather than method-scoped, because disposing IS the act here and the
        // assertions below have to run AFTER it. A bare `using var` would dispose at the end
        // of the method, i.e. after they had already read hal.Disposals.
        {
            using var enumerator = new MacOSAudioDeviceEnumerator(hal);
            IAudioCapture capture = enumerator.Open(Assert.Single(enumerator.List()));
            capture.Start();
        }

        Assert.Equal(1, hal.Disposals);
        // Still running, because nothing here released it. Had the enumerator disposed the
        // captures it opened, the IOProc would be gone and the capture's real owner would be
        // releasing something already released.
        Assert.Equal(1, hal.RunningIoProcs);
    }

    [Fact]
    public void Dispose_WhenThePlatformRefusesTheRelease_DoesNotThrow()
    {
        // Every caller reaches Dispose from a finally with no other owner to fall back on, so
        // the seam binds it not to throw. This is the only way to show that line is held: the
        // enumerator cannot verify it of a HAL it does not own, so it survives one that
        // misbehaves.
        var hal = new FakeCoreAudioHal
        {
            DisposeError = new CoreAudioException("removing the device-list listener", -66748),
        };
        var enumerator = new MacOSAudioDeviceEnumerator(hal);

        Assert.Null(Record.Exception(enumerator.Dispose));
        Assert.Equal(1, hal.Disposals);
    }

    [Fact]
    public void List_WhenTheDeviceTreeCannotBeWalked_ThrowsRatherThanAnsweringEmpty()
    {
        // Green when written, and here to STAY green. The seam is explicit that an empty list
        // means "no endpoints", never "the question could not be asked", and the tempting
        // defensive edit is a try/catch that returns [] so the picker does not blow up. That
        // would tell the operator their mic is gone when the audio service is merely down, and
        // the callers that survive a failed enumeration filter on this exception to tell those
        // apart.
        var hal = new FakeCoreAudioHal
        {
            ListDevicesError = new CoreAudioException("walking the device list", -66748),
        };
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        Assert.IsAssignableFrom<ExternalException>(Record.Exception(() => enumerator.List()));
    }
}
