using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>Well-known CoreAudio object ids.</summary>
public static class CoreAudioObject
{
    /// <summary><c>kAudioObjectSystemObject</c>: the singleton that owns the device list and
    /// the default-device properties. It is an <c>AudioObjectID</c> like any device, which is
    /// why a system-wide property listener rides the same call as a per-device one.</summary>
    public const uint System = 1;
}

/// <summary>
/// A property this backend watches for change notifications. Named after the CoreAudio
/// property each maps to, because mapping is all the HAL does with them; what a change
/// MEANS is decided above the facade.
/// </summary>
public enum CoreAudioPropertyKind
{
    /// <summary><c>kAudioDevicePropertyMute</c> on a device's input scope. Fires when the OS
    /// mutes or unmutes the endpoint; the listener re-reads the property, since the
    /// notification carries no state.</summary>
    Mute,

    /// <summary><c>kAudioHardwarePropertyDefaultInputDevice</c> on
    /// <see cref="CoreAudioObject.System"/>. Fires when the operator changes the system
    /// default input.</summary>
    DefaultInputDevice,

    /// <summary><c>kAudioDevicePropertyDeviceIsAlive</c> on a device. Fires when the endpoint
    /// goes away underneath a running capture (unplugged, disabled), which is the mid-stream
    /// loss <see cref="IAudioCapture.Failed"/> exists to surface.
    ///
    /// Declared here, not yet subscribed: the mid-stream half of <c>Failed</c> is the failure-
    /// signals slice, and raising it needs a decision this facade cannot make on its own,
    /// since the notification says the property CHANGED rather than which way. The Windows
    /// sibling declares the same seam member ahead of raising it, for the same
    /// reason.</summary>
    DeviceIsAlive,
}

/// <summary>
/// Receives one buffer of device-format PCM on the CoreAudio IO thread.
/// </summary>
/// <param name="audio">Interleaved bytes in the device's stream format, valid only for the
/// duration of the call: it is a window onto CoreAudio's own buffer, which is reused as soon
/// as the callback returns. A <see cref="ReadOnlySpan{T}"/> rather than a
/// <see cref="ReadOnlyMemory{T}"/> so that lifetime is enforced by the compiler instead of
/// documented, and so the facade itself never copies: whether to copy, and into what, is a
/// decision the capture above it makes.</param>
public delegate void CoreAudioIoCallback(ReadOnlySpan<byte> audio);

/// <summary>
/// An opaque handle to one registered IOProc. The HAL that created it owns whatever is
/// behind it (a real <c>AudioDeviceIOProcID</c> and the pin keeping the callback alive), and
/// a HAL rejects a handle it did not create, so a handle cannot be crossed between backends
/// or used after it is destroyed.
/// </summary>
public abstract class CoreAudioIoProcHandle;

/// <summary>
/// A CoreAudio call that failed, carrying the <c>OSStatus</c> it returned. Derives from
/// <see cref="ExternalException"/>, which is the failure type <see cref="IAudioCapture"/> and
/// <see cref="IAudioDeviceEnumerator"/> declare for a native/driver error, so the platform
/// layer satisfies the seam's contract rather than widening it: every caller that skips a
/// dead endpoint already filters on that type.
/// </summary>
public sealed class CoreAudioException : ExternalException
{
    /// <summary>The failed call, its <c>OSStatus</c> and what it was reaching for.</summary>
    /// <param name="what">The operation, in the words of whoever has to read the log.</param>
    /// <param name="status">The <c>OSStatus</c> the call returned. CoreAudio spells most of
    /// them as four-char codes ('!obj', 'stop'), so the raw value is kept verbatim rather
    /// than translated.</param>
    public CoreAudioException(string what, int status)
        : base($"{what} failed with OSStatus {status}", status) { }
}

/// <summary>
/// Every native call this backend makes, behind one facade.
///
/// It exists so that the decisions sit somewhere a test can reach. The Windows sibling
/// puts format classification, mute policy and teardown order inside
/// <c>WasapiCaptureBase</c>, a class that cannot be constructed without a real endpoint, so
/// none of it is exercised without hardware. Here the implementation is a dumb passthrough
/// carrying NO logic, covered only by a symbol smoke test and a manual on-Mac check, while
/// every judgement lives in the classes above and runs on the ubuntu lane.
///
/// The corollary binds anyone extending it: a method that decides something belongs above
/// this seam, not in it. If an implementation needs an <c>if</c> that is not a status check,
/// the facade is drawn in the wrong place.
///
/// Everything traffics in plain records and ids rather than native structs, so the fake is
/// constructible.
/// </summary>
public interface ICoreAudioHal : IDisposable
{
    /// <summary>Every audio device the system currently has, one row per device SCOPE that
    /// carries streams: CoreAudio has no notion of an input device, only of a device with
    /// input streams, so an interface with both appears twice, differing in
    /// <see cref="CoreAudioDevice.Flow"/>. Which of them the bridge is willing to tap is a
    /// decision for the enumerator above.</summary>
    /// <returns>The devices, in the order CoreAudio reports them.</returns>
    /// <exception cref="CoreAudioException">The device tree could not be walked.</exception>
    IReadOnlyList<CoreAudioDevice> ListDevices();

    /// <summary>The device's current input stream description.</summary>
    /// <param name="deviceId">The device's <c>AudioObjectID</c>.</param>
    /// <returns>The ASBD fields, uninterpreted.</returns>
    /// <exception cref="CoreAudioException">The property could not be read.</exception>
    CoreAudioStreamFormat ReadStreamFormat(uint deviceId);

    /// <summary>Whether the device is muted at the OS level right now.</summary>
    /// <param name="deviceId">The device's <c>AudioObjectID</c>.</param>
    /// <returns>The mute state, or <c>null</c> when the device does not carry the property at
    /// all. Null rather than false because the two are different facts and only the caller
    /// can decide what to do with "this endpoint has no mute to honour"; plenty of USB and
    /// virtual inputs answer that way.</returns>
    /// <exception cref="CoreAudioException">The device carries the property but reading it
    /// failed.</exception>
    bool? TryReadMute(uint deviceId);

    /// <summary>Watch <paramref name="kind"/> on <paramref name="objectId"/> until the
    /// returned registration is disposed.</summary>
    /// <param name="objectId">The <c>AudioObjectID</c> to watch: a device, or
    /// <see cref="CoreAudioObject.System"/> for a system-wide property.</param>
    /// <param name="kind">The property to watch.</param>
    /// <param name="handler">Invoked on a CoreAudio notification thread when the property
    /// changes. It carries no state, matching CoreAudio: the handler re-reads whatever it
    /// needs, so there is one source of truth rather than two that can disagree.</param>
    /// <returns>The registration. Disposing it removes the listener and must not throw, since
    /// every caller reaches it from a teardown path.</returns>
    /// <exception cref="CoreAudioException">The listener could not be added.</exception>
    IDisposable AddPropertyListener(uint objectId, CoreAudioPropertyKind kind, Action handler);

    /// <summary>Register an IOProc against the device. It does not run until
    /// <see cref="StartIo"/>.</summary>
    /// <param name="deviceId">The device's <c>AudioObjectID</c>.</param>
    /// <param name="callback">Invoked with each buffer while the IOProc runs.</param>
    /// <returns>The handle, valid until <see cref="DestroyIoProc"/>.</returns>
    /// <exception cref="CoreAudioException">The IOProc could not be created.</exception>
    CoreAudioIoProcHandle CreateIoProc(uint deviceId, CoreAudioIoCallback callback);

    /// <summary>Start the IOProc, after which its callback runs until <see cref="StopIo"/>.
    /// </summary>
    /// <param name="ioProc">A live handle from <see cref="CreateIoProc"/>.</param>
    /// <exception cref="CoreAudioException">The device refused to start.</exception>
    void StartIo(CoreAudioIoProcHandle ioProc);

    /// <summary>Stop the IOProc. Its callback has returned for the last time once this
    /// returns.</summary>
    /// <param name="ioProc">A live handle from <see cref="CreateIoProc"/>.</param>
    /// <exception cref="CoreAudioException">The device refused to stop, which is what an
    /// endpoint invalidated mid-capture does.</exception>
    void StopIo(CoreAudioIoProcHandle ioProc);

    /// <summary>Unregister the IOProc and release what backs the handle. The IOProc must
    /// already be stopped; CoreAudio refuses to destroy a running one.</summary>
    /// <param name="ioProc">A handle from <see cref="CreateIoProc"/>.</param>
    /// <exception cref="CoreAudioException">The IOProc could not be destroyed.</exception>
    void DestroyIoProc(CoreAudioIoProcHandle ioProc);
}

/// <summary>
/// One audio device scope, as CoreAudio describes it.
/// </summary>
/// <param name="ObjectId">The <c>AudioObjectID</c>. Every other HAL call takes it, and it is
/// deliberately NOT what <see cref="CaptureDevice.Id"/> carries: CoreAudio re-issues these
/// per boot and per replug, so a saved selection keyed on one names a different device (or
/// nothing) next launch.</param>
/// <param name="Uid">The device's persistent UID string. Stable across reboots and replugs,
/// which is what a saved device selection needs.</param>
/// <param name="Name">The device's human-readable name, for the picker.</param>
/// <param name="Flow">Whether this row is the device's input or output scope.</param>
/// <param name="IsDefault">True when this is the system default device for its flow.</param>
public sealed record CoreAudioDevice(
    uint ObjectId,
    string Uid,
    string Name,
    DeviceFlow Flow,
    bool IsDefault);
