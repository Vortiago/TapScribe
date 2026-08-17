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

        // Re-listed rather than remembered from the last List(): the picker's rows can be
        // minutes old, and the object id behind a UID changes on a replug, so a cached map
        // would open the wrong device or a dead one.
        CoreAudioDevice? found = _hal.ListDevices().FirstOrDefault(
            d => d.Flow == device.Flow
                && d.Flow == DeviceFlow.Capture
                && string.Equals(d.Uid, device.Id, StringComparison.Ordinal));

        // ArgumentException, the seam's clause for "the id names no active endpoint of the
        // requested flow". A vanished mic and an output endpoint are the same fact here:
        // nothing in the list matches. Not the native failure type, since the platform
        // answered fine and the callers that skip a dead endpoint filter on that one.
        if (found is null)
            throw new ArgumentException(
                $"no active capture endpoint with UID '{device.Id}' ({device.Name})", nameof(device));

        return new MacOSAudioCapture(_hal, found.ObjectId);
    }

    public void Dispose()
    {
        try
        {
            _hal.Dispose();
        }
        catch (CoreAudioException)
        {
            // CoreAudio refused to give a handle back, which is what a hardware layer already
            // torn down under us does. Swallowed because the seam binds this not to throw:
            // every caller reaches it from a finally with no other owner to fall back on, so a
            // throw here strands whatever the enumerator still holds for the process lifetime.
            // What is lost is the report, and there is nothing a caller could do with it: the
            // handles are gone either way.
        }
    }
}
