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

    /// <summary><c>kAudioDevicePropertyDeviceIsAlive</c> on a device. Fires when the endpoint
    /// leaves: unplugged, disabled, or an aggregate whose sub-device went away. Unlike
    /// <see cref="Mute"/> there is nothing to re-read, and deliberately no HAL call to read it
    /// with: a device that ARRIVES carries no listener yet, so a notification reaching one of
    /// these can only mean the object it names is gone.</summary>
    DeviceIsAlive,

    /// <summary><c>kAudioHardwarePropertyDefaultOutputDevice</c> on
    /// <see cref="CoreAudioObject.System"/>. Fires when the Mac starts playing through a
    /// different endpoint, which on this platform is what "the system audio moved" means: the
    /// listener re-reads the device list rather than trusting a payload, matching every other
    /// kind here.</summary>
    DefaultOutputDevice,

    // A kind goes in with the code that subscribes it, never ahead of it: each is a hand-typed
    // four-char selector, in the ONE class here no test can reach, whose whole bargain is being
    // too thin to need more than a symbol check.
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
/// An opaque handle to one live Core Audio process tap: the object that carries what the Mac
/// is playing. A tap is NOT something an IOProc can run over on its own - it becomes audio
/// only once it is listed inside an aggregate device (see
/// <see cref="ICoreAudioHal.CreateAggregateDevice"/>), which is why the two are separate
/// handles with separate lifetimes rather than one. Owned by the HAL that issued it, which
/// rejects a handle it did not create.
/// </summary>
public abstract class CoreAudioTapHandle;

/// <summary>
/// An opaque handle to one live aggregate device wrapping a process tap. Owned by the HAL that
/// issued it, which rejects a handle it did not create.
/// </summary>
public abstract class CoreAudioAggregateHandle
{
    /// <summary>The aggregate's <c>AudioObjectID</c>, which is what makes it an ordinary device
    /// to the rest of this seam: <see cref="ICoreAudioHal.CreateIoProc"/> and the property
    /// listeners take it like any other. Exposed rather than hidden because the whole point of
    /// the aggregate is to turn a tap into a device id.</summary>
    public abstract uint DeviceId { get; }
}

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

/// <summary>The handful of <c>OSStatus</c> values this backend raises on its own behalf, as
/// opposed to passing one back from a failed call. One home, because a hand-typed magic decimal
/// that is one digit wrong still filters correctly as an <see cref="ExternalException"/> and
/// logs something that means a different thing.</summary>
public static class CoreAudioStatus
{
    /// <summary><c>kAudioHardwareBadDeviceError</c>: the platform's own word for "the device you
    /// are asking about is not there", which is what both captures raise when the endpoint
    /// behind them goes away mid-stream.</summary>
    public const int BadDevice = 560227702;
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

    /// <summary>Create a tap on everything the Mac is playing: a stereo mixdown of every
    /// process, excluding none, left audible.
    ///
    /// Takes no arguments, and that is the seam's line rather than an omission. WHICH endpoint
    /// the tapped audio is collected through is a property of the aggregate device below, and
    /// deciding it (the current default output, rebound when it moves) is a judgement that
    /// lives above this facade; the tap itself has nothing to choose.</summary>
    /// <returns>The tap, valid until <see cref="DestroyProcessTap"/>.</returns>
    /// <exception cref="CoreAudioException">The tap could not be created.</exception>
    CoreAudioTapHandle CreateProcessTap();

    /// <summary>The tap's current stream description: what its audio actually is.</summary>
    /// <param name="tap">A live handle from <see cref="CreateProcessTap"/>.</param>
    /// <returns>The ASBD fields, uninterpreted.</returns>
    /// <exception cref="CoreAudioException">The property could not be read.</exception>
    CoreAudioStreamFormat ReadTapFormat(CoreAudioTapHandle tap);

    /// <summary>Release the tap. Its aggregate device must already be destroyed; the reverse
    /// order leaves an aggregate listing a tap that is gone.</summary>
    /// <param name="tap">A handle from <see cref="CreateProcessTap"/>.</param>
    /// <exception cref="CoreAudioException">The tap could not be destroyed.</exception>
    void DestroyProcessTap(CoreAudioTapHandle tap);

    /// <summary>Wrap <paramref name="tap"/> in a private aggregate device clocked by
    /// <paramref name="outputDeviceUid"/>, which is what gives the tap an
    /// <c>AudioObjectID</c> an IOProc can run over.</summary>
    /// <param name="outputDeviceUid">The output endpoint the aggregate is built around, by its
    /// persistent UID. Which endpoint that should be is decided above this facade.</param>
    /// <param name="tap">A live handle from <see cref="CreateProcessTap"/>.</param>
    /// <returns>The aggregate, valid until <see cref="DestroyAggregateDevice"/>.</returns>
    /// <exception cref="CoreAudioException">The aggregate could not be created.</exception>
    CoreAudioAggregateHandle CreateAggregateDevice(string outputDeviceUid, CoreAudioTapHandle tap);

    /// <summary>Release the aggregate. Any IOProc on it must already be destroyed.</summary>
    /// <param name="device">A handle from <see cref="CreateAggregateDevice"/>.</param>
    /// <exception cref="CoreAudioException">The aggregate could not be destroyed.</exception>
    void DestroyAggregateDevice(CoreAudioAggregateHandle device);
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
