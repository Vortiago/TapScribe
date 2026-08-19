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

    /// <summary>The id of the one row that stands for system audio.
    ///
    /// A UID rather than a real device's, because on macOS there is nothing to name: what the
    /// Mac plays is a process tap, and the tap follows whichever output is default rather than
    /// binding to an endpoint. Listing the real outputs instead would offer the operator rows
    /// that all open the same thing, so pinning "External Headphones" would silently record the
    /// default output, and a follow-default row plus a pinned one would run two taps recording
    /// one mixdown twice under two identities, which DuplicateIdentity cannot catch because the
    /// names differ. One row means a pin on it says exactly what following the default says.
    /// </summary>
    internal const string SystemAudioId = "tapscribe:system-audio";

    /// <summary>The name that row carries, matching the identity the runtime files it under.
    /// </summary>
    internal const string SystemAudioName = "System audio";

    // Inputs are real endpoints and are listed as they come. Output scopes are collapsed into
    // the single synthetic row above: they exist here only to answer "does this Mac play audio
    // at all", since a tap over no output is nothing.
    private IEnumerable<CoreAudioDevice> Inputs() =>
        _hal.ListDevices().Where(d => d.Flow == DeviceFlow.Capture);

    private bool HasOutput() => _hal.ListDevices().Any(d => d.Flow == DeviceFlow.Render);

    public IReadOnlyList<CaptureDevice> List()
    {
        List<CaptureDevice> rows = [.. Inputs().Select(Portable)];
        if (HasOutput())
            rows.Add(new CaptureDevice(SystemAudioId, SystemAudioName, DeviceFlow.Render, IsDefault: true));
        return rows;
    }

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

        // System audio first, because it names no endpoint: the tap binds to whichever output
        // is default and follows it, so there is no object id to resolve. Guarded on the Mac
        // still having an output, so a machine with none refuses rather than opening a tap over
        // nothing.
        if (device.Flow == DeviceFlow.Render && string.Equals(device.Id, SystemAudioId, StringComparison.Ordinal))
        {
            return HasOutput()
                ? new MacOSSystemAudioCapture(_hal)
                : throw new ArgumentException("this Mac has no audio output to tap", nameof(device));
        }

        // Re-listed rather than remembered from the last List(): the picker's rows can be
        // minutes old, and the object id behind a UID changes on a replug, so a cached map
        // would open the wrong device or a dead one.
        CoreAudioDevice? found = Inputs().FirstOrDefault(
            d => d.Flow == device.Flow && string.Equals(d.Uid, device.Id, StringComparison.Ordinal));

        // ArgumentException, the seam's clause for "the id names no active endpoint of the
        // requested flow". A vanished mic and a saved pin on a real output endpoint (which this
        // backend no longer offers) are the same fact here: nothing in the list matches. Not the
        // native failure type, since the platform answered fine and the callers that skip a dead
        // endpoint filter on that one.
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
