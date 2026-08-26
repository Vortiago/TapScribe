using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The real <see cref="ICoreAudioHal"/>: CoreAudio's HAL, reached by P/Invoke.
///
/// DELIBERATELY WITHOUT LOGIC. Every method is a property read, a registration or a status check,
/// and the only branch is "did the call succeed". Every judgement this backend makes (which layouts
/// are readable, what a clean stop means, what a missing mute property implies, teardown order)
/// lives in <see cref="CoreAudioFormat"/>, <see cref="MacOSAudioCapture"/> and
/// <see cref="MacOSAudioDeviceEnumerator"/>, unit-tested on a lane with no audio hardware. This
/// class is covered by <c>CoreAudioUpstreamContractTests</c> and a manual check on a Mac, which is
/// only acceptable while it stays this thin: an <c>if</c> here that is not a status check is a
/// decision that has escaped its test.
///
/// P/Invoke throughout, never the managed ObjC bindings, per the rule in
/// <see cref="MacOSProductVersion"/>. The one ObjC class the backend touches
/// (<c>CATapDescription</c>) is reached through the runtime's own C entry points in
/// <c>CoreAudioHal.Tap.cs</c>, which carries the process-tap surface (#420). Partial rather than a
/// second class so the upstream-contract test, which counts this type's <c>[LibraryImport]</c>s,
/// still sees every native declaration.
///
/// The native calls are CoreAudio's own to serialise; what this class adds is the four lists of what
/// it still holds, every touch of one under <c>_registrations</c>. Needed since #420: the
/// system-audio capture rebinds its tap from a CoreAudio NOTIFICATION thread while the tray thread
/// may be ending the meeting through the same HAL, and one enumerator's HAL is shared by every
/// capture it opened, so the race is ACROSS captures. The IOProc trampoline touches none of it, so
/// the realtime path pays nothing.
///
/// The macos platform attribute is on the TYPE, so "never touch a native symbol off a Mac" is
/// decided once, where this is CONSTRUCTED, and CA1416 proves it at compile time. A per-method guard
/// runs on no lane and has to invent an answer: an empty list from ListDevices tells the enumerator
/// "no endpoints" when the truth is "this host cannot be asked".
/// </summary>
[SupportedOSPlatform("macos")]
public sealed unsafe partial class CoreAudioHal : ICoreAudioHal
{
    /// <summary>Guards every list of what this HAL still holds. Taken by each method that
    /// reads or mutates one, so a caller never has to know which thread it is on.</summary>
    private readonly Lock _registrations = new();

    private readonly List<Registration> _listeners = [];
    private readonly List<IoProc> _ioProcs = [];
    private bool _disposed;

    // Static because the trampolines below are: CoreAudio hands them a raw pointer, not an
    // instance, so this counts faults across every HAL in the process, which is the question
    // worth asking anyway.
    private static long _callbackFaults;

    /// <summary>How many times a native callback swallowed an exception. Non-zero is the only trace
    /// either trampoline can leave: both MUST swallow, since an exception unwinding into CoreAudio
    /// tears the process down, and neither runs anywhere a report could go. Without this a handler
    /// throwing on EVERY call looks exactly like a device that never fires.</summary>
    internal static long CallbackFaults => Interlocked.Read(ref _callbackFaults);

    public IReadOnlyList<CoreAudioDevice> ListDevices()
    {
        uint[] ids = ReadUInt32Array(CoreAudioObject.System, Selector.Devices, Scope.Global);
        uint defaultInput = ReadDefaultDevice(Selector.DefaultInputDevice);
        uint defaultOutput = ReadDefaultDevice(Selector.DefaultOutputDevice);

        List<CoreAudioDevice> devices = [];
        foreach (uint id in ids)
        {
            string uid = ReadString(id, Selector.DeviceUid);
            string name = ReadString(id, Selector.Name);
            // One row per scope that actually carries streams. CoreAudio has no input device,
            // only a device with input streams, so an interface with both appears twice.
            if (ChannelCount(id, Scope.Input) > 0)
                devices.Add(new CoreAudioDevice(id, uid, name, DeviceFlow.Capture, id == defaultInput));
            if (ChannelCount(id, Scope.Output) > 0)
                devices.Add(new CoreAudioDevice(id, uid, name, DeviceFlow.Render, id == defaultOutput));
        }

        return devices;
    }

    public CoreAudioStreamFormat ReadStreamFormat(uint deviceId) =>
        ReadStreamDescription(
            deviceId, Selector.StreamFormat, Scope.Input,
            $"reading the stream format of device {deviceId}");

    // One ASBD read, shared by the device's input stream and the tap's own format: same
    // struct, same two-call shape, different object and scope.
    private static CoreAudioStreamFormat ReadStreamDescription(
        uint objectId, uint selector, uint scope, string what)
    {
        AudioStreamBasicDescription asbd = default;
        uint size = (uint)sizeof(AudioStreamBasicDescription);
        AudioObjectPropertyAddress address = Address(selector, scope);
        int status = AudioObjectGetPropertyData(objectId, &address, 0, IntPtr.Zero, &size, &asbd);
        if (status != NoError)
            throw new CoreAudioException(what, status);

        return new CoreAudioStreamFormat(
            asbd.SampleRate,
            (int)asbd.ChannelsPerFrame,
            (int)asbd.BitsPerChannel,
            asbd.FormatId,
            (CoreAudioFormatFlags)asbd.FormatFlags);
    }

    public bool? TryReadMute(uint deviceId)
    {
        AudioObjectPropertyAddress address = Address(Selector.Mute, Scope.Input);
        if (AudioObjectHasProperty(deviceId, &address) == 0)
            return null;

        uint muted = 0;
        uint size = sizeof(uint);
        int status = AudioObjectGetPropertyData(deviceId, &address, 0, IntPtr.Zero, &size, &muted);
        if (status != NoError)
            throw new CoreAudioException($"reading the mute state of device {deviceId}", status);

        return muted != 0;
    }

    public IDisposable AddPropertyListener(uint objectId, CoreAudioPropertyKind kind, Action handler)
    {
        ArgumentNullException.ThrowIfNull(handler);
        AudioObjectPropertyAddress address = kind switch
        {
            CoreAudioPropertyKind.Mute => Address(Selector.Mute, Scope.Input),
            CoreAudioPropertyKind.DeviceIsAlive => Address(Selector.DeviceIsAlive, Scope.Global),
            CoreAudioPropertyKind.DefaultOutputDevice =>
                Address(Selector.DefaultOutputDevice, Scope.Global),
            _ => throw new ArgumentOutOfRangeException(nameof(kind)),
        };

        var registration = new Registration(this, objectId, address, handler);
        int status = AudioObjectAddPropertyListener(
            objectId, &address, &OnPropertyChanged, GCHandle.ToIntPtr(registration.Pin));
        if (status != NoError)
        {
            registration.Unpin();
            throw new CoreAudioException($"watching a property on object {objectId}", status);
        }

        lock (_registrations)
            _listeners.Add(registration);
        return registration;
    }

    public CoreAudioIoProcHandle CreateIoProc(uint deviceId, CoreAudioIoCallback callback)
    {
        ArgumentNullException.ThrowIfNull(callback);
        var ioProc = new IoProc(deviceId, callback);
        IntPtr procId = IntPtr.Zero;
        int status = AudioDeviceCreateIOProcID(
            deviceId, &OnDeviceIo, GCHandle.ToIntPtr(ioProc.Pin), &procId);
        if (status != NoError || procId == IntPtr.Zero)
        {
            ioProc.Unpin();
            throw new CoreAudioException($"creating an IOProc on device {deviceId}", status);
        }

        ioProc.ProcId = procId;
        lock (_registrations)
            _ioProcs.Add(ioProc);
        return ioProc;
    }

    public void StartIo(CoreAudioIoProcHandle ioProc)
    {
        IoProc live = Live(ioProc, nameof(StartIo));
        int status = AudioDeviceStart(live.DeviceId, live.ProcId);
        if (status != NoError)
            throw new CoreAudioException($"starting the IOProc on device {live.DeviceId}", status);
    }

    public void StopIo(CoreAudioIoProcHandle ioProc)
    {
        IoProc live = Live(ioProc, nameof(StopIo));
        int status = AudioDeviceStop(live.DeviceId, live.ProcId);
        if (status != NoError)
            throw new CoreAudioException($"stopping the IOProc on device {live.DeviceId}", status);
    }

    public void DestroyIoProc(CoreAudioIoProcHandle ioProc)
    {
        IoProc live = Live(ioProc, nameof(DestroyIoProc));
        int status = AudioDeviceDestroyIOProcID(live.DeviceId, live.ProcId);
        // Unpinned only once CoreAudio says the registration is GONE. A failed destroy leaves the
        // IOProc registered, and the pin is what its client data points at: freeing it hands the
        // next callback a dangling GCHandle, a process-level access violation rather than a managed
        // throw the trampoline could contain. Leaving it listed costs at worst one pinned callback.
        if (status != NoError)
            throw new CoreAudioException($"destroying the IOProc on device {live.DeviceId}", status);

        lock (_registrations)
            _ioProcs.Remove(live);
        live.Unpin();
    }

    public void Dispose()
    {
        lock (_registrations)
        {
            if (_disposed)
                return;
            _disposed = true;
        }

        // The last owner of whatever is still registered, and nothing here throws: the seam binds
        // every release path to be throw-free and no caller is left to act on a failure. The
        // destroy's status is still read, for the reason DestroyIoProc gives: a pin freed behind a
        // registration CoreAudio still holds is a process-level fault.
        foreach (IoProc ioProc in Drain(_ioProcs))
        {
            AudioDeviceStop(ioProc.DeviceId, ioProc.ProcId);
            if (AudioDeviceDestroyIOProcID(ioProc.DeviceId, ioProc.ProcId) == NoError)
                ioProc.Unpin();
        }

        // Each Dispose removes the registration from the list, so the copy is what makes the
        // walk safe as well as what makes it exclusive.
        foreach (Registration registration in Drain(_listeners))
            registration.Dispose();

        // Last, because an aggregate device can be the very thing an IOProc above ran over.
        ReleaseTapObjects();
    }

    private IoProc Live(CoreAudioIoProcHandle ioProc, string call)
    {
        ArgumentNullException.ThrowIfNull(ioProc);
        if (ioProc is not IoProc live || !Holds(_ioProcs, live))
            throw new InvalidOperationException($"{call} was handed an IOProc handle this HAL does not hold");
        return live;
    }

    /// <summary>Whether this HAL still holds <paramref name="handle"/>, which is what makes a handle
    /// from another HAL, or one already released, refusable.</summary>
    private bool Holds<T>(List<T> held, T handle)
    {
        lock (_registrations)
            return held.Contains(handle);
    }

    /// <summary>Take everything a list holds and empty it, so the caller walks a snapshot nothing
    /// else can be releasing at the same time. The teardown shape: whoever drains owns what came
    /// out.</summary>
    private List<T> Drain<T>(List<T> held)
    {
        lock (_registrations)
        {
            List<T> taken = [.. held];
            held.Clear();
            return taken;
        }
    }

    // ---- property reads, all the same two-call shape: ask the size, then ask again ----

    private static uint[] ReadUInt32Array(uint objectId, uint selector, uint scope)
    {
        AudioObjectPropertyAddress address = Address(selector, scope);
        uint size = 0;
        int status = AudioObjectGetPropertyDataSize(objectId, &address, 0, IntPtr.Zero, &size);
        if (status != NoError)
            throw new CoreAudioException($"sizing property {selector:X} on object {objectId}", status);
        if (size == 0)
            return [];

        var values = new uint[size / sizeof(uint)];
        fixed (uint* target = values)
        {
            status = AudioObjectGetPropertyData(objectId, &address, 0, IntPtr.Zero, &size, target);
        }
        if (status != NoError)
            throw new CoreAudioException($"reading property {selector:X} on object {objectId}", status);

        // CoreAudio writes back what it actually filled, and the device list can shrink between the
        // sizing call and this one: unplug a mic in that window and the tail of the array is
        // untouched zeros, which the walk above would read as device id 0.
        int written = (int)(size / sizeof(uint));
        return written < values.Length ? values[..written] : values;
    }

    private static uint ReadDefaultDevice(uint selector)
    {
        AudioObjectPropertyAddress address = Address(selector, Scope.Global);
        uint deviceId = 0;
        uint size = sizeof(uint);
        int status = AudioObjectGetPropertyData(
            CoreAudioObject.System, &address, 0, IntPtr.Zero, &size, &deviceId);
        // A box with no default configured is not a failure, nor is a momentarily unreadable
        // property: 0 is kAudioObjectUnknown, which no device has. CaptureDevice.DefaultFor then
        // falls back to the first endpoint of the flow, the documented headless/RDP behaviour.
        return status == NoError ? deviceId : 0;
    }

    private static string ReadString(uint objectId, uint selector)
    {
        AudioObjectPropertyAddress address = Address(selector, Scope.Global);
        IntPtr cfString = IntPtr.Zero;
        uint size = (uint)IntPtr.Size;
        int status = AudioObjectGetPropertyData(objectId, &address, 0, IntPtr.Zero, &size, &cfString);
        // Status-checked like every other read here, rather than answered as "". This one produces
        // the device UID, the key a saved selection round-trips through, so "" for a failed read
        // means two unreadable devices collide on one id.
        if (status != NoError)
            throw new CoreAudioException($"reading property {selector:X} on object {objectId}", status);
        // noErr and no string is the property being present and empty: a device that named
        // itself nothing, which is a different fact from a read that failed.
        if (cfString == IntPtr.Zero)
            return "";

        try
        {
            // The C-string copy, not CFStringGetCStringPtr: that one is allowed to return null
            // whenever the string's internal encoding is not the one asked for, and does on
            // plenty of real device names.
            var buffer = new byte[MaxNameBytes];
            fixed (byte* target = buffer)
            {
                if (CFStringGetCString(cfString, target, buffer.Length, EncodingUtf8) == 0)
                    return "";
                // CFStringGetCString NUL-terminates on success, which is exactly the shape
                // PtrToStringUTF8 reads.
                return Marshal.PtrToStringUTF8((IntPtr)target) ?? "";
            }
        }
        finally
        {
            // The copy rule: AudioObjectGetPropertyData hands back a CFString the caller owns.
            CFRelease(cfString);
        }
    }

    // How many channels the device carries in this scope, which is how CoreAudio answers "does
    // it have input streams": there is no such flag, only a stream configuration to count.
    private static uint ChannelCount(uint deviceId, uint scope)
    {
        AudioObjectPropertyAddress address = Address(Selector.StreamConfiguration, scope);
        uint size = 0;
        int status = AudioObjectGetPropertyDataSize(deviceId, &address, 0, IntPtr.Zero, &size);
        // Status-checked rather than read as "no streams in this scope". A device CoreAudio refuses
        // to answer for is one the question could not be ASKED about, and answering 0 drops it from
        // the walk silently: the picker reports the mic as gone when the HAL refused. That is the
        // distinction ListDevices is declared to keep.
        if (status != NoError)
            throw new CoreAudioException(
                $"sizing the stream configuration of device {deviceId} in scope {scope:X}", status);
        // Too small to hold even a buffer count is a scope with nothing in it, which is the
        // honest zero.
        if (size < BufferListHeaderBytes)
            return 0;

        var raw = new byte[size];
        fixed (byte* target = raw)
        {
            status = AudioObjectGetPropertyData(deviceId, &address, 0, IntPtr.Zero, &size, target);
            if (status != NoError)
                throw new CoreAudioException(
                    $"reading the stream configuration of device {deviceId} in scope {scope:X}", status);

            // AudioBufferList is a count followed by that many AudioBuffers. The array starts at
            // offset 8, not 4: AudioBuffer holds a pointer, so it is 8-aligned and the count is
            // followed by four bytes of padding. Capped at what the returned size can hold, since
            // the count is native data and walking past it reads off the end.
            uint buffers = Math.Min(
                *(uint*)target, (size - BufferListHeaderBytes) / (uint)sizeof(AudioBuffer));
            uint channels = 0;
            for (uint i = 0; i < buffers; i++)
            {
                var buffer = (AudioBuffer*)(target + BufferListHeaderBytes + (i * sizeof(AudioBuffer)));
                channels += buffer->NumberChannels;
            }
            return channels;
        }
    }

    // ---- the native callbacks ----

    [UnmanagedCallersOnly]
    private static int OnPropertyChanged(uint objectId, uint addressCount, IntPtr addresses, IntPtr clientData)
    {
        try
        {
            if (GCHandle.FromIntPtr(clientData).Target is Registration registration)
                registration.Handler();
        }
        catch (Exception)
        {
            // Nothing may cross back into CoreAudio: an exception unwinding through a native frame
            // tears the process down. What is swallowed is a state refresh the next notification
            // redoes, and a notification thread has no channel to report on, so the count is the
            // trace it leaves.
            Interlocked.Increment(ref _callbackFaults);
        }
        return NoError;
    }

    [UnmanagedCallersOnly]
    private static int OnDeviceIo(
        uint deviceId,
        IntPtr now,
        IntPtr inputData,
        IntPtr inputTime,
        IntPtr outputData,
        IntPtr outputTime,
        IntPtr clientData)
    {
        try
        {
            if (inputData == IntPtr.Zero || GCHandle.FromIntPtr(clientData).Target is not IoProc ioProc)
                return NoError;

            var list = (byte*)inputData;
            if (*(uint*)list == 0)
                return NoError;

            // The FIRST buffer only. A multi-channel non-interleaved stream puts each channel in
            // its own buffer, and CoreAudioFormat refuses that layout at open time.
            var buffer = (AudioBuffer*)(list + BufferListHeaderBytes);
            if (buffer->Data != IntPtr.Zero && buffer->DataByteSize > 0)
                ioProc.Callback(new ReadOnlySpan<byte>((void*)buffer->Data, (int)buffer->DataByteSize));
        }
        catch (Exception)
        {
            // Same rule as above, and stricter: this is the realtime IO thread, so a handler that
            // throws costs one buffer of audio rather than the meeting. The increment is on the
            // failure path only, so the deadline pays nothing while things work.
            Interlocked.Increment(ref _callbackFaults);
        }
        return NoError;
    }

    /// <summary>One property-change registration, and the pin that lets the native callback
    /// find its handler.</summary>
    private sealed class Registration : IDisposable
    {
        private readonly CoreAudioHal _hal;
        private readonly uint _objectId;
        private AudioObjectPropertyAddress _address;
        private bool _unpinned;

        /// <summary>The handle handed to CoreAudio as client data.</summary>
        public GCHandle Pin { get; }

        /// <summary>What to run when the property changes.</summary>
        public Action Handler { get; }

        /// <summary>Pin this registration so a native callback can find it.</summary>
        public Registration(
            CoreAudioHal hal, uint objectId, AudioObjectPropertyAddress address, Action handler)
        {
            _hal = hal;
            _objectId = objectId;
            _address = address;
            Handler = handler;
            Pin = GCHandle.Alloc(this);
        }

        /// <summary>Release the pin without touching CoreAudio: the add failed, so there is no
        /// registration to remove.</summary>
        public void Unpin()
        {
            if (_unpinned)
                return;
            _unpinned = true;
            if (Pin.IsAllocated)
                Pin.Free();
        }

        public void Dispose()
        {
            if (_unpinned)
                return;
            int status;
            fixed (AudioObjectPropertyAddress* address = &_address)
                status = AudioObjectRemovePropertyListener(
                    _objectId, address, &OnPropertyChanged, GCHandle.ToIntPtr(Pin));

            // Not thrown (the seam binds a release not to throw) but not ignored either, for the
            // reason DestroyIoProc gives: the pin is this listener's client data, so freeing it
            // while CoreAudio still holds the registration hands the next notification a dangling
            // GCHandle. Left listed and pinned, so HAL.Dispose retries it once.
            if (status != NoError)
                return;

            lock (_hal._registrations)
                _hal._listeners.Remove(this);
            Unpin();
        }
    }

    /// <summary>One registered IOProc, and the pin that lets the native callback find its
    /// managed callback.</summary>
    private sealed class IoProc : CoreAudioIoProcHandle
    {
        private bool _unpinned;

        /// <summary>Pin this IOProc so the native callback can find it.</summary>
        public IoProc(uint deviceId, CoreAudioIoCallback callback)
        {
            DeviceId = deviceId;
            Callback = callback;
            Pin = GCHandle.Alloc(this);
        }

        /// <summary>The device this IOProc runs against.</summary>
        public uint DeviceId { get; }

        /// <summary>Where its buffers go.</summary>
        public CoreAudioIoCallback Callback { get; }

        /// <summary>The handle handed to CoreAudio as client data.</summary>
        public GCHandle Pin { get; }

        /// <summary>CoreAudio's <c>AudioDeviceIOProcID</c>, once created.</summary>
        public IntPtr ProcId { get; set; }

        /// <summary>Release the pin. After this no native callback may run, which is why every
        /// caller destroys or fails the registration first.</summary>
        public void Unpin()
        {
            if (_unpinned)
                return;
            _unpinned = true;
            if (Pin.IsAllocated)
                Pin.Free();
        }
    }

    private const int NoError = 0;


    // kCFStringEncodingUTF8.
    private const uint EncodingUtf8 = 0x08000100;

    // Device names and UIDs are short; this is slack, not a measurement.
    private const int MaxNameBytes = 1024;

    // sizeof(UInt32) plus four bytes of padding: AudioBuffer holds a pointer and is 8-aligned.
    private const int BufferListHeaderBytes = 8;

    private const string CoreAudioFramework = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio";

    private const string CoreFoundationFramework =
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation";

    /// <summary>The CoreAudio property selectors this backend names, as four-char codes.
    /// </summary>
    private static class Selector
    {
        public const uint Devices = 0x64657623;             // 'dev#'
        public const uint DefaultInputDevice = 0x64496E20;  // 'dIn '
        public const uint DefaultOutputDevice = 0x644F7574; // 'dOut'
        public const uint StreamConfiguration = 0x736C6179; // 'slay'
        public const uint StreamFormat = 0x73666D74;        // 'sfmt'
        public const uint DeviceUid = 0x75696420;           // 'uid '
        public const uint Name = 0x6C6E616D;                // 'lnam'
        public const uint Mute = 0x6D757465;                // 'mute'
        public const uint DeviceIsAlive = 0x6C69766E;       // 'livn'

        // Read off a TAP object rather than off a device: no AudioObjectID but a tap's
        // answers either of them.
        public const uint TapUid = 0x74756964;              // 'tuid'
        public const uint TapFormat = 0x74666D74;           // 'tfmt'
    }

    /// <summary>The CoreAudio property scopes, as four-char codes.</summary>
    private static class Scope
    {
        public const uint Global = 0x676C6F62; // 'glob'
        public const uint Input = 0x696E7074;  // 'inpt'
        public const uint Output = 0x6F757470; // 'outp'
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct AudioObjectPropertyAddress
    {
        public uint Selector;
        public uint Scope;
        public uint Element;
    }

    /// <summary>CoreAudio's <c>AudioStreamBasicDescription</c>, field for field.</summary>
    [StructLayout(LayoutKind.Sequential)]
    private struct AudioStreamBasicDescription
    {
        public double SampleRate;
        public uint FormatId;
        public uint FormatFlags;
        public uint BytesPerPacket;
        public uint FramesPerPacket;
        public uint BytesPerFrame;
        public uint ChannelsPerFrame;
        public uint BitsPerChannel;
        public uint Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct AudioBuffer
    {
        public uint NumberChannels;
        public uint DataByteSize;
        public IntPtr Data;
    }

    // kAudioObjectPropertyElementMain is 0.
    private static AudioObjectPropertyAddress Address(uint selector, uint scope) =>
        new() { Selector = selector, Scope = scope, Element = 0 };

    // ---- the native surface, and nothing else below this line ----

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioObjectGetPropertyDataSize(
        uint objectId,
        AudioObjectPropertyAddress* address,
        uint qualifierDataSize,
        IntPtr qualifierData,
        uint* dataSize);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioObjectGetPropertyData(
        uint objectId,
        AudioObjectPropertyAddress* address,
        uint qualifierDataSize,
        IntPtr qualifierData,
        uint* dataSize,
        void* data);

    [LibraryImport(CoreAudioFramework)]
    private static partial byte AudioObjectHasProperty(uint objectId, AudioObjectPropertyAddress* address);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioObjectAddPropertyListener(
        uint objectId,
        AudioObjectPropertyAddress* address,
        delegate* unmanaged<uint, uint, IntPtr, IntPtr, int> listener,
        IntPtr clientData);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioObjectRemovePropertyListener(
        uint objectId,
        AudioObjectPropertyAddress* address,
        delegate* unmanaged<uint, uint, IntPtr, IntPtr, int> listener,
        IntPtr clientData);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioDeviceCreateIOProcID(
        uint deviceId,
        delegate* unmanaged<uint, IntPtr, IntPtr, IntPtr, IntPtr, IntPtr, IntPtr, int> ioProc,
        IntPtr clientData,
        IntPtr* ioProcId);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioDeviceDestroyIOProcID(uint deviceId, IntPtr ioProcId);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioDeviceStart(uint deviceId, IntPtr ioProcId);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioDeviceStop(uint deviceId, IntPtr ioProcId);

    // bufferSize is a CFIndex, a signed long: nint, not int, the way every other CFIndex this
    // backend declares is spelled. A 32-bit argument leaves the top half of the register undefined
    // by the arm64 ABI, and the callee reads all 64 bits of it as the size of this buffer.
    [LibraryImport(CoreFoundationFramework)]
    private static partial byte CFStringGetCString(IntPtr theString, byte* buffer, nint bufferSize, uint encoding);

    [LibraryImport(CoreFoundationFramework)]
    private static partial void CFRelease(IntPtr cf);
}
