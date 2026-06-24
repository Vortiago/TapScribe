using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The one follow-default resolution rule, shared by <see cref="DeviceSelection.Resolve"/>
/// (what a meeting taps) and the Settings level meter (what it samples) — so the meter
/// always rides the exact endpoint the gate it's tuning will.
/// </summary>
public class CaptureDeviceTests
{
    private static CaptureDevice Dev(string id, DeviceFlow flow, bool isDefault) =>
        new(id, id, flow, isDefault);

    [Fact]
    public void DefaultFor_PrefersTheFlowsDefaultEndpoint()
    {
        var devices = new[]
        {
            Dev("mic-a", DeviceFlow.Capture, isDefault: false),
            Dev("mic-b", DeviceFlow.Capture, isDefault: true),
            Dev("spk", DeviceFlow.Render, isDefault: true),
        };

        Assert.Equal("mic-b", CaptureDevice.DefaultFor(devices, DeviceFlow.Capture)?.Id);
    }

    [Fact]
    public void DefaultFor_FallsBackToTheFirstOfTheFlow_WhenNoneIsDefault()
    {
        // Headless / RDP boxes report no default yet still have active endpoints.
        var devices = new[]
        {
            Dev("spk", DeviceFlow.Render, isDefault: false),
            Dev("mic-a", DeviceFlow.Capture, isDefault: false),
            Dev("mic-b", DeviceFlow.Capture, isDefault: false),
        };

        Assert.Equal("mic-a", CaptureDevice.DefaultFor(devices, DeviceFlow.Capture)?.Id);
    }

    [Fact]
    public void DefaultFor_IgnoresOtherFlows_AndReturnsNullWhenNonePresent()
    {
        var renderOnly = new[] { Dev("spk", DeviceFlow.Render, isDefault: true) };

        Assert.Null(CaptureDevice.DefaultFor(renderOnly, DeviceFlow.Capture));
    }
}
