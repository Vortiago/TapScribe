using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The device-enumeration seam (<see cref="IAudioDeviceEnumerator"/>) is a thin
/// cross-platform contract; the real listing lives in the Windows WASAPI impl and
/// is verified on a Windows box, not here. These pin the contract shape the
/// orchestrator and the fake enumerator depend on.
/// </summary>
public class DeviceEnumerationTests
{
    private static AudioFormat Mic => new(16_000, 1, SampleKind.Int16);
    private static AudioFormat RenderMix => new(48_000, 2, SampleKind.Float32); // typical loopback mix

    [Fact]
    public void List_ReturnsCaptureDevices_AndLoopbackCapableRenderDevices()
    {
        var devices = new FakeAudioDeviceEnumerator();
        devices.Add(new CaptureDevice("mic-1", "Microphone", DeviceFlow.Capture, IsDefault: true), Mic);
        devices.Add(new CaptureDevice("spk-1", "Speakers", DeviceFlow.Render, IsDefault: true), RenderMix);

        IReadOnlyList<CaptureDevice> listed = devices.List();

        Assert.Contains(listed, d => d.Flow == DeviceFlow.Capture);
        Assert.Contains(listed, d => d.Flow == DeviceFlow.Render); // the loopback candidate
    }

    [Fact]
    public void Open_ReturnsACapture_WithTheDevicesFormat()
    {
        var devices = new FakeAudioDeviceEnumerator();
        var speakers = new CaptureDevice("spk-1", "Speakers", DeviceFlow.Render, IsDefault: true);
        devices.Add(speakers, RenderMix);

        IAudioCapture capture = devices.Open(speakers);

        Assert.Equal(RenderMix, capture.Format); // the rest of the pipeline consumes this unchanged
    }
}
