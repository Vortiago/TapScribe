using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// A scripted <see cref="ICoreAudioHal"/> that VALIDATES rather than merely records (#419).
///
/// A double that logs calls would let the capture above it start a destroyed IOProc, destroy
/// a running one, or leak a listener, and every test would still pass; the whole reason the
/// facade exists is that those are the mistakes hardware would catch and a unit test
/// otherwise would not. So this one tracks handle lifetime, refuses a handle it did not
/// issue, refuses the orderings CoreAudio itself refuses, and lets a test push PCM into a
/// running IOProc or fire any registered listener.
/// </summary>
internal sealed class FakeCoreAudioHal : ICoreAudioHal
{
    private readonly List<CoreAudioDevice> _devices = [];
    private readonly Dictionary<uint, CoreAudioStreamFormat> _formats = [];
    private readonly Dictionary<uint, bool?> _mute = [];
    private readonly List<Registration> _listeners = [];
    private readonly List<Handle> _handles = [];
    private readonly List<Tap> _taps = [];
    private readonly List<AggregateDevice> _aggregates = [];

    /// <summary>When set, <see cref="ListDevices"/> throws it.</summary>
    public Exception? ListDevicesError { get; set; }

    /// <summary>When set, <see cref="ReadStreamFormat"/> throws it.</summary>
    public Exception? ReadStreamFormatError { get; set; }

    /// <summary>When set, <see cref="CreateIoProc"/> throws it: the device refused the
    /// registration, which is the native error a capture has to surface as the seam's declared
    /// type.</summary>
    public Exception? CreateIoProcError { get; set; }

    /// <summary>When set, <see cref="StartIo"/> throws it.</summary>
    public Exception? StartIoError { get; set; }

    /// <summary>When set, <see cref="StopIo"/> throws it: the endpoint was invalidated while
    /// capture ran, so there is nothing left to stop.</summary>
    public Exception? StopIoError { get; set; }

    /// <summary>When set, <see cref="CreateProcessTap"/> throws it: this Mac refused the tap,
    /// which is what a missing TCC grant and a pre-14.4 kernel both look like from here.
    /// </summary>
    public Exception? CreateProcessTapError { get; set; }

    /// <summary>When set, <see cref="CreateAggregateDevice"/> throws it.</summary>
    public Exception? CreateAggregateDeviceError { get; set; }

    /// <summary>When set, <see cref="ReadTapFormat"/> throws it: the tap was built and then
    /// refused to say what it carries, which is the one step of a rebind that runs AFTER both
    /// system-wide objects exist and so is the one whose unwind has something to release.
    /// </summary>
    public Exception? ReadTapFormatError { get; set; }

    /// <summary>When set, <see cref="Dispose"/> throws it. The seam binds disposal to be
    /// throw-free all the way up, and only a HAL that misbehaves can show that the layer above
    /// holds that line.</summary>
    public Exception? DisposeError { get; set; }

    /// <summary>When set, <see cref="TryReadMute"/> answers the FIRST read and throws it on
    /// every one after. The probe for "does this endpoint carry a mute at all" and the seed of
    /// its current state are two separate reads, and a real device can answer the first and
    /// refuse the second; that is the one shape in which a capture's constructor fails with a
    /// subscription already taken.</summary>
    public Exception? SeedMuteError { get; set; }

    /// <summary>How many times this HAL was released. Counted rather than flagged: whoever
    /// owns it is contract-bound to be throw-free, never to be idempotent.</summary>
    public int Disposals { get; private set; }

    /// <summary>Register a device. <paramref name="mute"/> null models an endpoint that does
    /// not carry the mute property at all, which is the majority of USB and virtual
    /// inputs.</summary>
    /// <param name="device">The device to report from <see cref="ListDevices"/>.</param>
    /// <param name="format">The stream description <see cref="ReadStreamFormat"/> answers
    /// with.</param>
    /// <param name="mute">The initial mute state, or null for no mute property.</param>
    /// <returns>The same device, so a caller can register and name it in one line.</returns>
    public CoreAudioDevice AddDevice(
        CoreAudioDevice device, CoreAudioStreamFormat? format = null, bool? mute = null)
    {
        ArgumentNullException.ThrowIfNull(device);
        _devices.Add(device);
        _formats[device.ObjectId] = format ?? Formats.Float32Stereo48k;
        _mute[device.ObjectId] = mute;
        return device;
    }

    /// <summary>Flip the OS-level mute and fire the property, the way a real endpoint does the
    /// two together. Only meaningful for a device registered WITH mute support; muting one
    /// without it is what the OS cannot do, so it is rejected here rather than silently
    /// modelled.</summary>
    /// <param name="deviceId">The device to mute or unmute.</param>
    /// <param name="muted">The new state.</param>
    public void SetMuted(uint deviceId, bool muted)
    {
        if (!_mute.TryGetValue(deviceId, out bool? current))
            throw new InvalidOperationException($"device {deviceId} was never added");
        if (current is null)
            throw new InvalidOperationException(
                $"device {deviceId} carries no mute property, so the OS cannot change it");

        _mute[deviceId] = muted;
        FireProperty(deviceId, CoreAudioPropertyKind.Mute);
    }

    /// <summary>Move the system default output to <paramref name="device"/> and fire the
    /// property, the way plugging in headphones does the two together. Registering the device
    /// as part of the move rather than beforehand is what makes a test read as the situation:
    /// the endpoint appears and becomes the default in one step.</summary>
    /// <param name="device">The endpoint that is now the default output. Must be a
    /// <see cref="DeviceFlow.Render"/> row; there is no such thing as a default output that is
    /// an input.</param>
    public void SetDefaultOutput(CoreAudioDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);
        if (device.Flow != DeviceFlow.Render)
            throw new InvalidOperationException($"{device.Name} is not an output, so it cannot be the default one");

        for (int i = 0; i < _devices.Count; i++)
            if (_devices[i].Flow == DeviceFlow.Render)
                _devices[i] = _devices[i] with { IsDefault = false };

        int existing = _devices.FindIndex(d => d.ObjectId == device.ObjectId && d.Flow == DeviceFlow.Render);
        if (existing >= 0)
            _devices[existing] = device with { IsDefault = true };
        else
            AddDevice(device with { IsDefault = true });

        FireProperty(CoreAudioObject.System, CoreAudioPropertyKind.DefaultOutputDevice);
    }

    /// <summary>Invoke every live listener on <paramref name="objectId"/> for
    /// <paramref name="kind"/>. A no-op when nothing is watching, matching CoreAudio: the OS
    /// fires on properties nobody asked about, and a capture that registered no listener is
    /// exactly what should hear nothing.</summary>
    /// <param name="objectId">The object whose property changed.</param>
    /// <param name="kind">The property that changed.</param>
    public void FireProperty(uint objectId, CoreAudioPropertyKind kind)
    {
        // Snapshot: a handler is entitled to dispose its own registration, which mutates the
        // list we are walking.
        foreach (Registration listener in _listeners
            .Where(l => l.Live && l.ObjectId == objectId && l.Kind == kind)
            .ToList())
        {
            listener.Invoke();
        }
    }

    /// <summary>How many live listeners are watching <paramref name="kind"/> on
    /// <paramref name="objectId"/>. The claim a leak test makes, and the claim a
    /// "registered nothing at all" test makes.</summary>
    /// <param name="objectId">The object to count listeners on.</param>
    /// <param name="kind">The property to count listeners for.</param>
    /// <returns>The count of registrations added and not yet disposed.</returns>
    public int ListenerCount(uint objectId, CoreAudioPropertyKind kind) =>
        _listeners.Count(l => l.Live && l.ObjectId == objectId && l.Kind == kind);

    /// <summary>Total live listeners, whatever they watch: what disposing the owner has to
    /// bring to zero.</summary>
    public int LiveListeners => _listeners.Count(l => l.Live);

    /// <summary>IOProcs created and not yet destroyed.</summary>
    public int LiveIoProcs => _handles.Count(h => !h.Destroyed);

    /// <summary>IOProcs currently running.</summary>
    public int RunningIoProcs => _handles.Count(h => h.Running);

    /// <summary>Process taps created and not yet destroyed.</summary>
    public int LiveTaps => _taps.Count(t => !t.Destroyed);

    /// <summary>Aggregate devices created and not yet destroyed.</summary>
    public int LiveAggregates => _aggregates.Count(a => !a.Destroyed);

    /// <summary>The output endpoint each live aggregate was built around, by UID. What a
    /// rebind test reads: the claim is not "an aggregate exists" but "the aggregate is bound to
    /// the device the Mac is playing through NOW".</summary>
    public IReadOnlyList<string> LiveAggregateOutputs =>
        [.. _aggregates.Where(a => !a.Destroyed).Select(a => a.OutputDeviceUid)];

    /// <summary>The stream description every tap reports. Fixed for the whole fake rather than
    /// per tap, because a global tap has one format at a time and the thing worth varying is
    /// what it becomes AFTER a rebind - which is what setting this between binds models.
    /// </summary>
    public CoreAudioStreamFormat TapFormat { get; set; } = Formats.Float32Stereo48k;

    /// <summary>Deliver one buffer to the device's running IOProc, the stand-in for CoreAudio
    /// calling it. Refuses when nothing is running: a real IOProc's callback cannot fire
    /// before <see cref="StartIo"/> or after <see cref="StopIo"/>, so a test that pushes into
    /// a stopped device is asserting about something that never happens.</summary>
    /// <param name="deviceId">The device to deliver to.</param>
    /// <param name="pcm">Device-format bytes.</param>
    public void PushAudio(uint deviceId, byte[] pcm)
    {
        ArgumentNullException.ThrowIfNull(pcm);
        Handle handle = _handles.Find(h => h.DeviceId == deviceId && h.Running)
            ?? throw new InvalidOperationException(
                $"device {deviceId} has no running IOProc, so CoreAudio would deliver nothing");

        handle.Callback(pcm);
    }

    /// <summary>Runs at the top of <see cref="ListDevices"/>. The rebind calls it while holding
    /// the capture's binding lock, so it is where a test can act with that lock held.</summary>
    public Action? BeforeListDevices { get; set; }

    public IReadOnlyList<CoreAudioDevice> ListDevices()
    {
        BeforeListDevices?.Invoke();
        return ListDevicesError is not null ? throw ListDevicesError : _devices.ToList();
    }

    public CoreAudioStreamFormat ReadStreamFormat(uint deviceId)
    {
        if (ReadStreamFormatError is not null)
            throw ReadStreamFormatError;
        return _formats.TryGetValue(deviceId, out CoreAudioStreamFormat? format)
            ? format
            : throw new CoreAudioException($"reading the stream format of device {deviceId}", NoSuchObject);
    }

    public bool? TryReadMute(uint deviceId)
    {
        if (SeedMuteError is not null && _muteReads++ > 0)
            throw SeedMuteError;
        return _mute.TryGetValue(deviceId, out bool? muted) ? muted : null;
    }

    public IDisposable AddPropertyListener(uint objectId, CoreAudioPropertyKind kind, Action handler)
    {
        ArgumentNullException.ThrowIfNull(handler);
        var registration = new Registration(objectId, kind, handler);
        _listeners.Add(registration);
        return registration;
    }

    public CoreAudioIoProcHandle CreateIoProc(uint deviceId, CoreAudioIoCallback callback)
    {
        ArgumentNullException.ThrowIfNull(callback);
        if (CreateIoProcError is not null)
            throw CreateIoProcError;

        var handle = new Handle(deviceId, callback);
        _handles.Add(handle);
        return handle;
    }

    public void StartIo(CoreAudioIoProcHandle ioProc)
    {
        Handle handle = Live(ioProc, nameof(StartIo));
        if (handle.Running)
            throw new InvalidOperationException("the IOProc is already running");
        if (StartIoError is not null)
            throw StartIoError;
        handle.Running = true;
    }

    public void StopIo(CoreAudioIoProcHandle ioProc)
    {
        Handle handle = Live(ioProc, nameof(StopIo));
        // Stopping a stopped IOProc is legal on real CoreAudio (it answers noErr), and the
        // seam documents Stop as safe to call when not started, so this does not refuse it.
        //
        // Cleared BEFORE the throw, so a scripted StopIoError still leaves DestroyIoProc able to
        // succeed. Whether the real HAL agrees is UNKNOWN: a stop that fails because the endpoint
        // is gone would likely refuse the destroy too, but nothing here can force a real stop to
        // fail, so modelling that would be a guess dressed as a fixture. Left honest rather than
        // plausible; the teardown-fault tests above it assert this fake's rule, not CoreAudio's.
        handle.Running = false;
        if (StopIoError is not null)
            throw StopIoError;
    }

    public void DestroyIoProc(CoreAudioIoProcHandle ioProc)
    {
        Handle handle = Live(ioProc, nameof(DestroyIoProc));
        if (handle.Running)
            throw new InvalidOperationException(
                "the IOProc is still running; CoreAudio refuses to destroy one that was not stopped first");
        handle.Destroyed = true;
    }

    public CoreAudioTapHandle CreateProcessTap()
    {
        if (CreateProcessTapError is not null)
            throw CreateProcessTapError;

        var tap = new Tap($"tap-uid-{_tapsCreated++}");
        _taps.Add(tap);
        return tap;
    }

    public CoreAudioStreamFormat ReadTapFormat(CoreAudioTapHandle tap)
    {
        LiveTap(tap, nameof(ReadTapFormat));
        return ReadTapFormatError is null ? TapFormat : throw ReadTapFormatError;
    }

    public void DestroyProcessTap(CoreAudioTapHandle tap)
    {
        Tap live = LiveTap(tap, nameof(DestroyProcessTap));
        // The ordering CoreAudio itself enforces, and the one a teardown is easiest to get
        // backwards: an aggregate that still lists this tap refers to an object that is gone.
        if (_aggregates.Any(a => !a.Destroyed && a.Tap == live))
            throw new InvalidOperationException(
                "the tap is still listed by a live aggregate device; destroy the aggregate first");
        live.Destroyed = true;
    }

    public CoreAudioAggregateHandle CreateAggregateDevice(string outputDeviceUid, CoreAudioTapHandle tap)
    {
        ArgumentNullException.ThrowIfNull(outputDeviceUid);
        Tap live = LiveTap(tap, nameof(CreateAggregateDevice));
        if (CreateAggregateDeviceError is not null)
            throw CreateAggregateDeviceError;

        var aggregate = new AggregateDevice(_nextAggregateId++, outputDeviceUid, live);
        _aggregates.Add(aggregate);
        return aggregate;
    }

    public void DestroyAggregateDevice(CoreAudioAggregateHandle device)
    {
        AggregateDevice live = LiveAggregate(device, nameof(DestroyAggregateDevice));
        if (_handles.Any(h => !h.Destroyed && h.DeviceId == live.DeviceId))
            throw new InvalidOperationException(
                "an IOProc is still registered on the aggregate; destroy it first");
        live.Destroyed = true;
    }

    public void Dispose()
    {
        Disposals++;
        if (DisposeError is not null)
            throw DisposeError;
    }

    // kAudioHardwareBadObjectError, the four-char code '!obj'.
    private const int NoSuchObject = 560947818;

    // Mute reads answered so far, which is what SeedMuteError counts against.
    private int _muteReads;

    // Taps issued so far, so each gets a distinguishable UID the way CoreAudio's do.
    private int _tapsCreated;

    // Aggregate object ids, from well above any device id a test registers, so a test that
    // confuses the two fails rather than silently addressing a real device.
    private uint _nextAggregateId = 900;

    // The handle-validating half: a handle has to be one THIS fake issued and still live, or
    // the call is one hardware would have refused.
    private Handle Live(CoreAudioIoProcHandle ioProc, string call)
    {
        ArgumentNullException.ThrowIfNull(ioProc);
        if (ioProc is not Handle handle || !_handles.Contains(handle))
            throw new InvalidOperationException($"{call} was handed an IOProc handle this HAL never issued");
        if (handle.Destroyed)
            throw new InvalidOperationException($"{call} was handed an IOProc handle that was already destroyed");
        return handle;
    }

    private Tap LiveTap(CoreAudioTapHandle tap, string call)
    {
        ArgumentNullException.ThrowIfNull(tap);
        if (tap is not Tap live || !_taps.Contains(live))
            throw new InvalidOperationException($"{call} was handed a tap handle this HAL never issued");
        if (live.Destroyed)
            throw new InvalidOperationException($"{call} was handed a tap handle that was already destroyed");
        return live;
    }

    private AggregateDevice LiveAggregate(CoreAudioAggregateHandle device, string call)
    {
        ArgumentNullException.ThrowIfNull(device);
        if (device is not AggregateDevice live || !_aggregates.Contains(live))
            throw new InvalidOperationException(
                $"{call} was handed an aggregate handle this HAL never issued");
        if (live.Destroyed)
            throw new InvalidOperationException(
                $"{call} was handed an aggregate handle that was already destroyed");
        return live;
    }

    private sealed class Tap(string uid) : CoreAudioTapHandle
    {
        public string Uid { get; } = uid;
        public bool Destroyed { get; set; }
    }

    private sealed class AggregateDevice(uint deviceId, string outputDeviceUid, Tap tap)
        : CoreAudioAggregateHandle
    {
        public override uint DeviceId { get; } = deviceId;
        public string OutputDeviceUid { get; } = outputDeviceUid;
        public Tap Tap { get; } = tap;
        public bool Destroyed { get; set; }
    }

    private sealed class Handle(uint deviceId, CoreAudioIoCallback callback) : CoreAudioIoProcHandle
    {
        public uint DeviceId { get; } = deviceId;
        public CoreAudioIoCallback Callback { get; } = callback;
        public bool Running { get; set; }
        public bool Destroyed { get; set; }
    }

    private sealed class Registration(uint objectId, CoreAudioPropertyKind kind, Action handler) : IDisposable
    {
        // What makes removal WAIT for a callback that is already running, which is the half of
        // AudioObjectRemovePropertyListener's contract a caller can deadlock against.
        private readonly Lock _inFlight = new();

        public uint ObjectId { get; } = objectId;
        public CoreAudioPropertyKind Kind { get; } = kind;
        public Action Handler { get; } = handler;
        public bool Live { get; private set; } = true;

        public void Invoke()
        {
            lock (_inFlight)
                Handler();
        }

        // Reentrant, so a handler disposing its own registration still returns. A DIFFERENT
        // thread's removal blocks here until the callback it raced returns.
        public void Dispose()
        {
            Live = false;
            lock (_inFlight) { }
        }
    }
}

/// <summary>Stream descriptions the capture tests hand the fake, so a test that is not ABOUT
/// the format does not have to spell one.</summary>
internal static class Formats
{
    /// <summary>What almost every Mac input reports: packed 32-bit float, 48 kHz, stereo.
    /// </summary>
    public static CoreAudioStreamFormat Float32Stereo48k { get; } = new(
        SampleRate: 48_000,
        ChannelsPerFrame: 2,
        BitsPerChannel: 32,
        FormatId: CoreAudioFormatId.LinearPcm,
        FormatFlags: CoreAudioFormatFlags.IsFloat | CoreAudioFormatFlags.IsPacked);
}

/// <summary>Devices the capture and enumerator tests register, named so a test reads as the
/// situation it is about rather than as five positional arguments.</summary>
internal static class Devices
{
    /// <summary>An input device.</summary>
    /// <param name="objectId">Its CoreAudio object id.</param>
    /// <param name="name">Its display name; the UID is derived from it.</param>
    /// <param name="isDefault">Whether it is the system default input.</param>
    /// <returns>The device row.</returns>
    public static CoreAudioDevice Input(uint objectId, string name, bool isDefault = false) =>
        new(objectId, $"{name}:uid", name, DeviceFlow.Capture, isDefault);

    /// <summary>An output device: what this slice's enumerator must NOT hand out as a mic.
    /// </summary>
    /// <param name="objectId">Its CoreAudio object id.</param>
    /// <param name="name">Its display name; the UID is derived from it.</param>
    /// <param name="isDefault">Whether it is the system default output.</param>
    /// <returns>The device row.</returns>
    public static CoreAudioDevice Output(uint objectId, string name, bool isDefault = false) =>
        new(objectId, $"{name}:uid", name, DeviceFlow.Render, isDefault);
}
