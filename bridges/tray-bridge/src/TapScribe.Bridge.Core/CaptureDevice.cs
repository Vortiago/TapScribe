namespace TapScribe.Bridge.Core;

/// <summary>
/// Whether an audio endpoint records input or plays output. A
/// <see cref="Render"/> endpoint is the loopback candidate: capturing it (WASAPI
/// loopback on Windows) records the system audio out — the "other side" of a meeting.
/// </summary>
public enum DeviceFlow
{
    /// <summary>An input endpoint — a microphone or line-in.</summary>
    Capture,

    /// <summary>An output endpoint — speakers/headphones, captured via loopback.</summary>
    Render,
}

/// <summary>
/// A platform-neutral descriptor of one audio endpoint the bridge can tap. The
/// cross-platform core depends only on this and <see cref="IAudioDeviceEnumerator"/>;
/// the WASAPI / MMDevice details stay in the Windows project, so nothing
/// Windows-specific leaks above the <see cref="IAudioCapture"/> seam.
/// </summary>
/// <param name="Id">Stable endpoint id, round-trippable through
/// <see cref="IAudioDeviceEnumerator.Open"/> (the WASAPI MMDevice id on Windows).</param>
/// <param name="Name">Human-readable device name for the picker (FriendlyName).</param>
/// <param name="Flow">Capture (mic) or Render (loopback candidate).</param>
/// <param name="IsDefault">True for the system default endpoint of its flow.</param>
public sealed record CaptureDevice(string Id, string Name, DeviceFlow Flow, bool IsDefault)
{
    /// <summary>
    /// The follow-default endpoint for <paramref name="flow"/> among
    /// <paramref name="devices"/>: the flow's default, or — when no default is configured
    /// (headless / RDP / freshly provisioned boxes report none yet still have active
    /// endpoints) — the first device of that flow, or <c>null</c> if there is none. The one
    /// place that rule lives, shared by <see cref="DeviceSelection.Resolve"/> (what a
    /// meeting taps) and the Settings level meter (what it samples), so the meter always
    /// rides the exact endpoint the gate it's tuning will.
    /// </summary>
    public static CaptureDevice? DefaultFor(IReadOnlyList<CaptureDevice> devices, DeviceFlow flow)
    {
        ArgumentNullException.ThrowIfNull(devices);
        return devices.FirstOrDefault(d => d.Flow == flow && d.IsDefault)
            ?? devices.FirstOrDefault(d => d.Flow == flow);
    }
}
