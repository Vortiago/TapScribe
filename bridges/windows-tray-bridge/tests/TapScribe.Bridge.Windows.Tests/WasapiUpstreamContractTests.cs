using System.Reflection;
using NAudio.CoreAudioApi;
using NAudio.Wave;

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
    public void EnumValues_ForFlowStateAndRole_Exist()
    {
        // These compile only if the members exist with these names; assert they're
        // defined values of their enums so an upstream rename trips here, not at runtime.
        Assert.True(Enum.IsDefined(DataFlow.Capture));
        Assert.True(Enum.IsDefined(DataFlow.Render));
        Assert.True(Enum.IsDefined(DeviceState.Active));
        Assert.True(Enum.IsDefined(Role.Multimedia));
    }
}
