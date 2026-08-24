using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>Well-known CoreAudio object ids.</summary>
public static class CoreAudioObject
{
    /// <summary><c>kAudioObjectSystemObject</c>: the singleton owning the device list and the
    /// default-device properties. An <c>AudioObjectID</c> like any device, which is why a
    /// system-wide property listener rides the same call as a per-device one.</summary>
    public const uint System = 1;
}

/// <summary>
/// A property this backend watches. Named after the CoreAudio property each maps to, because
/// mapping is all the HAL does with them; what a change MEANS is decided above the facade.
/// </summary>
public enum CoreAudioPropertyKind
{
    /// <summary><c>kAudioDevicePropertyMute</c> on a device's input scope. The notification carries
    /// no state, so the listener re-reads.</summary>
    Mute,

    /// <summary><c>kAudioDevicePropertyDeviceIsAlive</c>. Fires when the endpoint leaves: unplugged,
    /// disabled, or an aggregate whose sub-device went away. Nothing to re-read, and deliberately no
    /// HAL call to read it with: a device that ARRIVES carries no listener, so a notification here
    /// can only mean the object is gone.</summary>
    DeviceIsAlive,

    /// <summary><c>kAudioHardwarePropertyDefaultOutputDevice</c> on
    /// <see cref="CoreAudioObject.System"/>. The Mac started playing through a different endpoint,
    /// which is what "the system audio moved" means here.</summary>
    DefaultOutputDevice,

    // A kind goes in with the code that subscribes it, never ahead of it: each is a hand-typed
    // four-char selector, in the one class here no test can reach.
}

/// <summary>
/// Receives one buffer of device-format PCM on the CoreAudio IO thread.
/// </summary>
/// <param name="audio">Interleaved bytes in the device's stream format, valid only for the duration
/// of the call: a window onto CoreAudio's own buffer, reused as soon as the callback returns. A
/// span rather than a <see cref="ReadOnlyMemory{T}"/> so the compiler enforces that lifetime, and so
/// the facade never copies: whether to copy, and into what, is the capture's decision.</param>
public delegate void CoreAudioIoCallback(ReadOnlySpan<byte> audio);

/// <summary>
/// An opaque handle to one registered IOProc. The issuing HAL owns what is behind it (an
/// <c>AudioDeviceIOProcID</c> and the pin keeping the callback alive) and rejects a handle it did
/// not create, so a handle cannot be crossed between backends or used after it is destroyed.
/// </summary>
public abstract class CoreAudioIoProcHandle;

/// <summary>
/// An opaque handle to one live Core Audio process tap: the object carrying what the Mac is playing.
/// A tap is not something an IOProc can run over on its own. It becomes audio only once it is listed
/// inside an aggregate device (<see cref="ICoreAudioHal.CreateAggregateDevice"/>), which is why the
/// two are separate handles with separate lifetimes.
/// </summary>
public abstract class CoreAudioTapHandle;

/// <summary>
/// An opaque handle to one live aggregate device wrapping a process tap.
/// </summary>
public abstract class CoreAudioAggregateHandle
{
    /// <summary>The aggregate's <c>AudioObjectID</c>, which is what makes it an ordinary device to
    /// the rest of this seam. Exposed rather than hidden because turning a tap into a device id is
    /// the whole point of the aggregate.</summary>
    public abstract uint DeviceId { get; }
}

/// <summary>
/// A CoreAudio call that failed, carrying its <c>OSStatus</c>. Derives from
/// <see cref="ExternalException"/>, the failure type <see cref="IAudioCapture"/> and
/// <see cref="IAudioDeviceEnumerator"/> declare for a native error, so the platform layer satisfies
/// the seam's contract rather than widening it.
/// </summary>
public sealed class CoreAudioException : ExternalException
{
    /// <param name="what">The operation, in the words of whoever has to read the log.</param>
    /// <param name="status">The <c>OSStatus</c>, kept verbatim: CoreAudio spells most of them as
    /// four-char codes ('!obj', 'stop').</param>
    public CoreAudioException(string what, int status)
        : base($"{what} failed with OSStatus {status}", status) { }
}

/// <summary>The <c>OSStatus</c> values this backend raises on its own behalf, as opposed to passing
/// one back from a failed call. One home, because a hand-typed magic decimal that is one digit wrong
/// still filters correctly as an <see cref="ExternalException"/> and logs something else.</summary>
public static class CoreAudioStatus
{
    /// <summary><c>kAudioHardwareBadDeviceError</c>: what both captures raise when the endpoint
    /// behind them goes away mid-stream.</summary>
    public const int BadDevice = 560227702;
}

/// <summary>
/// Every native call this backend makes, behind one facade, so the decisions sit somewhere a test
/// can reach. The implementation is a dumb passthrough carrying NO logic, covered by a symbol smoke
/// test and a manual on-Mac check; every judgement lives in the classes above it and runs on the
/// ubuntu lane. (The Windows sibling puts format classification, mute policy and teardown order
/// inside <c>WasapiCaptureBase</c>, which cannot be constructed without a real endpoint.)
///
/// The corollary binds anyone extending it: a method that decides something belongs above this seam.
/// If an implementation needs an <c>if</c> that is not a status check, the facade is drawn in the
/// wrong place.
///
/// Everything traffics in plain records and ids rather than native structs, so the fake is
/// constructible.
/// </summary>
public interface ICoreAudioHal : IDisposable
{
    /// <summary>Every audio device the system currently has, one row per device SCOPE that carries
    /// streams: CoreAudio has no notion of an input device, only of a device with input streams, so
    /// an interface with both appears twice, differing in <see cref="CoreAudioDevice.Flow"/>. Which
    /// of them the bridge will tap is the enumerator's decision.</summary>
    /// <exception cref="CoreAudioException">The device tree could not be walked.</exception>
    IReadOnlyList<CoreAudioDevice> ListDevices();

    /// <summary>The device's current input stream description, uninterpreted.</summary>
    /// <exception cref="CoreAudioException">The property could not be read.</exception>
    CoreAudioStreamFormat ReadStreamFormat(uint deviceId);

    /// <summary>Whether the device is muted at the OS level right now.</summary>
    /// <returns>The mute state, or <c>null</c> when the device carries no mute property. Null rather
    /// than false because the two are different facts and only the caller can decide what to do with
    /// "this endpoint has no mute to honour"; plenty of USB and virtual inputs answer that way.
    /// </returns>
    /// <exception cref="CoreAudioException">The device carries the property but reading it failed.
    /// </exception>
    bool? TryReadMute(uint deviceId);

    /// <summary>Watch <paramref name="kind"/> on <paramref name="objectId"/> (a device, or
    /// <see cref="CoreAudioObject.System"/>) until the returned registration is disposed. The handler
    /// runs on a CoreAudio notification thread and carries no state, matching CoreAudio: it re-reads
    /// what it needs, so there is one source of truth rather than two that can disagree. Disposing
    /// must not throw, since every caller reaches it from a teardown path.</summary>
    /// <exception cref="CoreAudioException">The listener could not be added.</exception>
    IDisposable AddPropertyListener(uint objectId, CoreAudioPropertyKind kind, Action handler);

    /// <summary>Register an IOProc against the device. It does not run until
    /// <see cref="StartIo"/>.</summary>
    /// <exception cref="CoreAudioException">The IOProc could not be created.</exception>
    CoreAudioIoProcHandle CreateIoProc(uint deviceId, CoreAudioIoCallback callback);

    /// <summary>Start the IOProc, after which its callback runs until <see cref="StopIo"/>.</summary>
    /// <exception cref="CoreAudioException">The device refused to start.</exception>
    void StartIo(CoreAudioIoProcHandle ioProc);

    /// <summary>Stop the IOProc. Its callback has returned for the last time once this returns.
    /// </summary>
    /// <exception cref="CoreAudioException">The device refused to stop, which is what an endpoint
    /// invalidated mid-capture does.</exception>
    void StopIo(CoreAudioIoProcHandle ioProc);

    /// <summary>Unregister the IOProc and release what backs the handle. It must already be stopped;
    /// CoreAudio refuses to destroy a running one.</summary>
    /// <exception cref="CoreAudioException">The IOProc could not be destroyed.</exception>
    void DestroyIoProc(CoreAudioIoProcHandle ioProc);

    /// <summary>Create a tap on everything the Mac is playing: a stereo mixdown of every process,
    /// excluding none, left audible.
    ///
    /// Takes no arguments, and that is the seam's line rather than an omission. WHICH endpoint the
    /// tapped audio is collected through is a property of the aggregate device below, and deciding
    /// it (the current default output, rebound when it moves) lives above this facade.</summary>
    /// <exception cref="CoreAudioException">The tap could not be created.</exception>
    CoreAudioTapHandle CreateProcessTap();

    /// <summary>The tap's current stream description: what its audio actually is.</summary>
    /// <exception cref="CoreAudioException">The property could not be read.</exception>
    CoreAudioStreamFormat ReadTapFormat(CoreAudioTapHandle tap);

    /// <summary>Release the tap. Its aggregate device must already be destroyed; the reverse order
    /// leaves an aggregate listing a tap that is gone.</summary>
    /// <exception cref="CoreAudioException">The tap could not be destroyed.</exception>
    void DestroyProcessTap(CoreAudioTapHandle tap);

    /// <summary>Wrap <paramref name="tap"/> in a private aggregate device clocked by
    /// <paramref name="outputDeviceUid"/>, which is what gives the tap an <c>AudioObjectID</c> an
    /// IOProc can run over. Which endpoint that should be is decided above this facade.</summary>
    /// <exception cref="CoreAudioException">The aggregate could not be created.</exception>
    CoreAudioAggregateHandle CreateAggregateDevice(string outputDeviceUid, CoreAudioTapHandle tap);

    /// <summary>Release the aggregate. Any IOProc on it must already be destroyed.</summary>
    /// <exception cref="CoreAudioException">The aggregate could not be destroyed.</exception>
    void DestroyAggregateDevice(CoreAudioAggregateHandle device);
}

/// <summary>
/// One audio device scope, as CoreAudio describes it.
/// </summary>
/// <param name="ObjectId">The <c>AudioObjectID</c>. Every other HAL call takes it, and it is
/// deliberately NOT what <see cref="CaptureDevice.Id"/> carries: CoreAudio re-issues these per boot
/// and per replug, so a saved selection keyed on one names a different device next launch.</param>
/// <param name="Uid">The persistent UID, stable across reboots and replugs, which is what a saved
/// device selection needs.</param>
/// <param name="Name">The human-readable name, for the picker.</param>
/// <param name="Flow">Whether this row is the device's input or output scope.</param>
/// <param name="IsDefault">True when this is the system default device for its flow.</param>
public sealed record CoreAudioDevice(
    uint ObjectId,
    string Uid,
    string Name,
    DeviceFlow Flow,
    bool IsDefault);
