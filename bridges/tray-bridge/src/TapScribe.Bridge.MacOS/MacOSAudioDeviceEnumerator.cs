using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS <see cref="IAudioDeviceEnumerator"/>: lists the endpoints a meeting can tap and
/// opens one, as the microphone capture or as the system-audio tap. It owns the
/// <see cref="ICoreAudioHal"/> it was built with and hands it to every capture it opens,
/// which is why the seam requires it to OUTLIVE them.
///
/// Both flows, like the Windows sibling, but for a different reason underneath: there a render
/// endpoint IS the loopback client, and here it is the stand-in for a Core Audio process tap,
/// which has no endpoint of its own (#420). What the seam promises - open a
/// <see cref="DeviceFlow.Render"/> row and get the system audio - holds either way, which is
/// what lets <c>BridgeRuntime</c> resolve the operator's two selections without knowing which
/// platform it is on.
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

    // Every scope the HAL reports, which is inputs and outputs both. One predicate, used by
    // List and Open alike, so what the picker OFFERS and what Open ACCEPTS are the same rule by
    // construction rather than by two edits staying in step.
    private IEnumerable<CoreAudioDevice> Tappable() => _hal.ListDevices();

    public IReadOnlyList<CaptureDevice> List() => [.. Tappable().Select(Portable)];

    /// <summary>The portable descriptor for one CoreAudio device row.
    ///
    /// Keyed on the UID rather than the <c>AudioObjectID</c>: CoreAudio re-issues object ids
    /// per boot and per replug, so a saved device selection keyed on one names a different
    /// device next launch, or nothing. <see cref="Open"/> resolves the UID back.
    ///
    /// Shared with <see cref="MacOSSystemAudioCapture"/>, which finds the default output
    /// through Core's own <see cref="CaptureDevice.DefaultFor"/> rule: mapped in one place so
    /// the endpoint the tap binds to and the one the picker shows cannot be arrived at
    /// differently.</summary>
    /// <param name="device">The row as the HAL reported it.</param>
    /// <returns>The same device, in the terms the core speaks.</returns>
    internal static CaptureDevice Portable(CoreAudioDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);
        return new CaptureDevice(device.Uid, device.Name, device.Flow, device.IsDefault);
    }

    public IAudioCapture Open(CaptureDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);

        // Re-listed rather than remembered from the last List(): the picker's rows can be
        // minutes old, and the object id behind a UID changes on a replug, so a cached map
        // would open the wrong device or a dead one.
        CoreAudioDevice? found = Tappable().FirstOrDefault(
            d => d.Flow == device.Flow && string.Equals(d.Uid, device.Id, StringComparison.Ordinal));

        // ArgumentException, the seam's clause for "the id names no active endpoint of the
        // requested flow". A vanished mic and an output endpoint are the same fact here:
        // nothing in the list matches. Not the native failure type, since the platform
        // answered fine and the callers that skip a dead endpoint filter on that one.
        if (found is null)
            throw new ArgumentException(
                $"no active capture endpoint with UID '{device.Id}' ({device.Name})", nameof(device));

        // A render row names system audio rather than an endpoint to open: what the Mac plays
        // goes to whichever output is default, and the tap binds to that itself (see
        // MacOSSystemAudioCapture). So the row is resolved above to confirm it is a live
        // endpoint of the requested flow, which is what makes a stale saved pin an
        // ArgumentException here rather than a tap on the wrong thing, and then the object id
        // is deliberately not used.
        return found.Flow == DeviceFlow.Render
            ? new MacOSSystemAudioCapture(_hal)
            : new MacOSAudioCapture(_hal, found.ObjectId);
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
