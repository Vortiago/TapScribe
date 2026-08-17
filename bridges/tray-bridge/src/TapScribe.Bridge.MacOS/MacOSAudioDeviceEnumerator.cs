using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS <see cref="IAudioDeviceEnumerator"/>: lists the input endpoints the bridge can
/// tap and opens one as a <see cref="MacOSAudioCapture"/>. It owns the
/// <see cref="ICoreAudioHal"/> it was built with and hands it to every capture it opens,
/// which is why the seam requires it to OUTLIVE them.
/// </summary>
public sealed class MacOSAudioDeviceEnumerator : IAudioDeviceEnumerator
{
    private readonly ICoreAudioHal _hal;

    /// <summary>Build an enumerator over <paramref name="hal"/>, which it then owns.
    /// </summary>
    /// <param name="hal">The facade over CoreAudio. Released by
    /// <see cref="Dispose"/>.</param>
    public MacOSAudioDeviceEnumerator(ICoreAudioHal hal)
    {
        ArgumentNullException.ThrowIfNull(hal);
        _hal = hal;
    }

    public IReadOnlyList<CaptureDevice> List() =>
        _hal.ListDevices()
            // Inputs only. The HAL reports every device scope, and an output reaching this
            // list would offer the picker an endpoint Open cannot honour: capturing system
            // audio on macOS is a process tap rather than a device (#420), so there is no
            // loopback flow here to answer with. The Windows sibling lists both because
            // WASAPI loopback IS a render endpoint.
            .Where(d => d.Flow == DeviceFlow.Capture)
            // Keyed on the UID rather than the AudioObjectID: CoreAudio re-issues object ids
            // per boot and per replug, so a saved device selection keyed on one names a
            // different device next launch, or nothing. Open resolves the UID back.
            .Select(d => new CaptureDevice(d.Uid, d.Name, d.Flow, d.IsDefault))
            .ToList();

    public IAudioCapture Open(CaptureDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);
        throw new NotImplementedException();
    }

    public void Dispose() => _hal.Dispose();
}
