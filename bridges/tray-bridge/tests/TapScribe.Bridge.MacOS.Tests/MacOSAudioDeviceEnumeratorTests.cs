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
    public void List_OffersBothFlows_EachWithItsOwnDefault()
    {
        // The HAL reports every device scope, each carrying its OWN default flag, and both
        // flows reach the picker: a render endpoint is what a meeting's system-audio selection
        // resolves against, since capturing it on macOS is a process tap rather than a device
        // (#420). Without a render row here, the default device pair resolves half-way and
        // EVERY Start posts "Some devices unavailable / Skipped: default system audio".
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone"));
        hal.AddDevice(Devices.Input(57, "Yeti Nano", isDefault: true));
        hal.AddDevice(Devices.Output(63, "MacBook Pro Speakers", isDefault: true));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        IReadOnlyList<CaptureDevice> devices = enumerator.List();

        Assert.Equal(
            ["Built-in Microphone", "Yeti Nano", "MacBook Pro Speakers"], devices.Select(d => d.Name));
        Assert.Equal(DeviceFlow.Render, devices.Single(d => d.Name == "MacBook Pro Speakers").Flow);
        Assert.Equal("Yeti Nano", CaptureDevice.DefaultFor(devices, DeviceFlow.Capture)?.Name);
        Assert.Equal("MacBook Pro Speakers", CaptureDevice.DefaultFor(devices, DeviceFlow.Render)?.Name);
    }

    [Fact]
    public void Open_ARenderEndpoint_GivesTheSystemAudioTap()
    {
        // The seam's Render case, which on Windows is a WASAPI loopback client and here is a
        // process tap inside an aggregate device. What the caller gets back is an IAudioCapture
        // either way, which is the whole point of the seam: BridgeRuntime opens the operator's
        // two selections identically and never learns which platform it is on.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Output(63, "MacBook Pro Speakers", isDefault: true));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);

        using IAudioCapture capture = enumerator.Open(Assert.Single(enumerator.List()));

        capture.Start();
        Assert.Equal(1, hal.LiveTaps);
        Assert.Equal(1, hal.LiveAggregates);
        Assert.Equal(1, hal.RunningIoProcs);
    }

    [Fact]
    public void DefaultDeviceSelection_ResolvesWholeAgainstThisEnumerator()
    {
        // The bug this pins is what a first launch looked like: the default pair is a
        // follow-default microphone and a follow-default system audio, and with no render row
        // in the list the second one resolved to nothing. So every Start posted "Some devices
        // unavailable / Skipped: default system audio" and recorded one speaker, on a Mac that
        // was working perfectly. Asserted through the same two calls BridgeRuntime makes, so it
        // is the operator's path rather than a restatement of the filter.
        var hal = new FakeCoreAudioHal();
        hal.AddDevice(Devices.Input(41, "Built-in Microphone", isDefault: true));
        hal.AddDevice(Devices.Output(63, "MacBook Pro Speakers", isDefault: true));
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);
        var settings = new BridgeSettings { Identity = "atle" };

        ResolveResult resolution = DeviceSelection.Resolve(
            settings.EffectiveDevices, enumerator.List(), "atle");

        Assert.Empty(resolution.Missing);
        Assert.Equal(SelectionVerdict.Ok, resolution.Verdict);
        Assert.Equal(
            ["Built-in Microphone", "MacBook Pro Speakers"],
            resolution.Resolved.Select(r => r.Device.Name));
        // Two speakers under two identities, which is what the Recorder attributes the meeting
        // by: the operator, and everyone else.
        Assert.Equal(["atle", "System audio"], resolution.Resolved.Select(r => r.StreamingIdentity));
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

    // Every refusal here is the seam's ArgumentException clause, "the id names no active
    // endpoint of the requested flow", because on this backend they are the same fact: nothing
    // in the list matches. ExternalException would be wrong for any of them, since the platform
    // answered fine, and the callers that skip a dead endpoint filter on that one.
    public static TheoryData<string, CaptureDevice> UnopenableDevices() => new()
    {
        {
            "a UID no listed device carries",
            new CaptureDevice("unplugged-since-the-picker-was-drawn:uid", "Ghost", DeviceFlow.Capture, false)
        },
        {
            "an output UID that is no longer here",
            new CaptureDevice("Unplugged Headphones:uid", "Unplugged Headphones", DeviceFlow.Render, false)
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
