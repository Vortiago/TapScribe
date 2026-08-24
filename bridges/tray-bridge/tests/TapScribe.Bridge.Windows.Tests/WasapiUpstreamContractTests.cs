using System.Reflection;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// Pins the undocumented NAudio symbols the WASAPI backends bind to, so an NAudio
/// bump that renames or moves one fails THIS test at CI time instead of at first tap
/// on an operator's machine — the same upstream-contract discipline CLAUDE.md
/// prescribes for the MLX adapters. Reflection-only: it asserts shape without opening
/// a real audio endpoint (CI runners have none), and the NAudio package is pinned to
/// an exact version (2.2.1) as the primary defence.
/// </summary>
public class WasapiUpstreamContractTests
{
    [Fact]
    public void WasapiCapture_HasDefaultAndMMDeviceConstructors()
    {
        Assert.NotNull(typeof(WasapiCapture).GetConstructor(Type.EmptyTypes));     // default mic
        Assert.NotNull(typeof(WasapiCapture).GetConstructor([typeof(MMDevice)]));   // specific mic
    }

    [Fact]
    public void WasapiLoopbackCapture_IsAWasapiCapture_WithDefaultAndMMDeviceConstructors()
    {
        Assert.True(typeof(WasapiCapture).IsAssignableFrom(typeof(WasapiLoopbackCapture)));
        Assert.NotNull(typeof(WasapiLoopbackCapture).GetConstructor(Type.EmptyTypes));    // default render
        Assert.NotNull(typeof(WasapiLoopbackCapture).GetConstructor([typeof(MMDevice)])); // specific render
    }

    [Fact]
    public void WasapiCapture_ExposesWaveFormatDataAvailableAndLifecycle()
    {
        Assert.NotNull(typeof(WasapiCapture).GetProperty("WaveFormat"));
        EventInfo? dataAvailable = typeof(WasapiCapture).GetEvent("DataAvailable");
        Assert.NotNull(dataAvailable);
        Assert.Equal(typeof(EventHandler<WaveInEventArgs>), dataAvailable!.EventHandlerType);
        Assert.NotNull(typeof(WasapiCapture).GetMethod("StartRecording", Type.EmptyTypes));
        Assert.NotNull(typeof(WasapiCapture).GetMethod("StopRecording", Type.EmptyTypes));
    }

    [Fact]
    public void MMDeviceEnumerator_ExposesEndpointEnumerationAndDefaults()
    {
        Assert.NotNull(typeof(MMDeviceEnumerator).GetMethod(
            "EnumerateAudioEndPoints", [typeof(DataFlow), typeof(DeviceState)]));
        Assert.NotNull(typeof(MMDeviceEnumerator).GetMethod("GetDevice", [typeof(string)]));
        Assert.NotNull(typeof(MMDeviceEnumerator).GetMethod(
            "GetDefaultAudioEndpoint", [typeof(DataFlow), typeof(Role)]));
        Assert.NotNull(typeof(MMDeviceEnumerator).GetMethod(
            "HasDefaultAudioEndpoint", [typeof(DataFlow), typeof(Role)]));
    }

    [Fact]
    public void MMDevice_ExposesIdFriendlyName_AndIsDisposable()
    {
        Assert.NotNull(typeof(MMDevice).GetProperty("ID"));
        Assert.NotNull(typeof(MMDevice).GetProperty("FriendlyName"));
        Assert.True(typeof(IDisposable).IsAssignableFrom(typeof(MMDevice)));
    }

    [Fact]
    public void MMDevice_ExposesEndpointVolume_WithMuteAndNotification()
    {
        // The mic mute-awareness path (#159): WasapiCaptureBase reads
        // MMDevice.AudioEndpointVolume.Mute and subscribes to OnVolumeNotification.
        // Pin every symbol it binds so an NAudio rename trips here, not at first tap.
        PropertyInfo? endpointVolume = typeof(MMDevice).GetProperty("AudioEndpointVolume");
        Assert.NotNull(endpointVolume);
        Assert.Equal(typeof(AudioEndpointVolume), endpointVolume!.PropertyType);

        PropertyInfo? mute = typeof(AudioEndpointVolume).GetProperty("Mute");
        Assert.NotNull(mute);
        Assert.Equal(typeof(bool), mute!.PropertyType);

        EventInfo? notification = typeof(AudioEndpointVolume).GetEvent("OnVolumeNotification");
        Assert.NotNull(notification);
        // The delegate carries the muted state we read in the callback.
        MethodInfo invoke = notification!.EventHandlerType!.GetMethod("Invoke")!;
        Type dataType = invoke.GetParameters().Single().ParameterType;
        Assert.Equal(typeof(AudioVolumeNotificationData), dataType);

        PropertyInfo? muted = typeof(AudioVolumeNotificationData).GetProperty("Muted");
        Assert.NotNull(muted);
        Assert.Equal(typeof(bool), muted!.PropertyType);
    }

    [Fact]
    public void EnumValues_ForFlowStateAndRole_Exist()
    {
        // These compile only if the members exist with these names; assert they're
        // defined values of their enums so an upstream rename trips here, not at runtime.
        Assert.True(Enum.IsDefined(DataFlow.Capture));
        Assert.True(Enum.IsDefined(DataFlow.Render));
        Assert.True(Enum.IsDefined(DeviceState.Active));
        Assert.True(Enum.IsDefined(Role.Multimedia));
    }

    [RequiresWindows("walk the real WASAPI endpoint tree")]
    public void List_OnTheRunningWindows_CompletesAndAnswersWellFormedRows()
    {
        // Reflection above says the symbols EXIST; this says the calls work. Everything else in
        // this project asserts shape, and everything above the seam runs against a fake, so the
        // enumerator's actual walk - two default-endpoint probes plus an EnumerateAudioEndPoints
        // per flow, each row read for ID and FriendlyName - is otherwise reached for the first
        // time on an operator's machine. The macOS twin
        // (CoreAudioUpstreamContractTests.ListDevices_OnTheRunningMac_...) is this test.
        //
        // Deliberately does NOT assert "found some": a CI runner has no audio endpoint, and
        // failing for that would be a fact about the box rather than about the code. What a
        // runner buys either way is the two default probes and both enumerations; a box WITH
        // endpoints additionally buys the per-row reads and the mute probe below.
        using var enumerator = new WasapiDeviceEnumerator();
        IReadOnlyList<CaptureDevice> devices = enumerator.List();

        Assert.All(devices, device =>
        {
            Assert.False(string.IsNullOrWhiteSpace(device.Id), "an endpoint came back with no id");
            Assert.False(string.IsNullOrWhiteSpace(device.Name), $"{device.Id} came back with no name");
            Assert.True(Enum.IsDefined(device.Flow));
        });

        // At most one default per flow. The enumerator reads the default id once per flow and
        // stamps rows against it, so two defaults would mean the stamp is comparing the wrong
        // thing, and CaptureDevice.DefaultFor would then pick whichever came back first.
        Assert.All(
            devices.GroupBy(d => d.Flow),
            flow => Assert.True(
                flow.Count(d => d.IsDefault) <= 1,
                $"{flow.Key} reported {flow.Count(d => d.IsDefault)} default endpoints"));
    }

    [RequiresWindows("read a real endpoint's OS mute state")]
    public void AnEndpointsMute_ReadsThroughAudioEndpointVolume_OrSaysTheEndpointHasNone()
    {
        // The other native path with a managed object behind it: WasapiCaptureBase caches an
        // AudioEndpointVolume per capture and subscribes OnVolumeNotification to it, and a mute
        // it cannot read is a gate that hard-closes on a device recording perfectly well. The
        // reflection test above proves the property exists; only this reaches the COM call.
        //
        // No-ops on a runner with no endpoints, which is the honest outcome there.
        using var mm = new MMDeviceEnumerator();
        foreach (MMDevice device in mm.EnumerateAudioEndPoints(DataFlow.All, DeviceState.Active))
        {
            using (device)
            {
                // The value is whatever the operator's mixer says; that it can be READ is the
                // claim, and a throw here is the failure.
                _ = device.AudioEndpointVolume.Mute;
            }
        }
    }
}
