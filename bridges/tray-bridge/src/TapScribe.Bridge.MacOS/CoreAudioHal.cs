using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The real <see cref="ICoreAudioHal"/>: CoreAudio's HAL, reached by P/Invoke.
///
/// DELIBERATELY WITHOUT LOGIC. Every method is a property read, a registration or a status
/// check, and the only branch is "did the call succeed". Every
/// judgement this backend makes (which layouts are readable, what a clean stop means, what a
/// missing mute property implies, teardown order) lives in <see cref="CoreAudioFormat"/>,
/// <see cref="MacOSAudioCapture"/> and <see cref="MacOSAudioDeviceEnumerator"/>, which have no
/// native dependency and are unit-tested on a lane with no audio hardware. This class is
/// covered by <c>CoreAudioUpstreamContractTests</c> and a manual check on a Mac, and that is
/// only an acceptable bargain while it stays this thin: an <c>if</c> here that is not a status
/// check is a decision that has escaped its test, and belongs above the facade.
///
/// P/Invoke throughout, never the managed ObjC bindings, per the rule in
/// <see cref="MacOSProductVersion"/>. Nothing here is an ObjC class, so no
/// <c>objc_msgSend</c> is needed either: the HAL is a plain C API.
///
/// Not thread-safe, and does not need to be: one enumerator owns one of these and drives it
/// from the tray thread. The native CALLBACKS arrive on CoreAudio threads, but they touch only
/// their own pinned registration, never the lists here.
///
/// The macos platform attribute is on the TYPE, so the "never touch a native symbol off a Mac"
/// rule is decided once, where this is CONSTRUCTED, and CA1416 proves it at compile time.
/// Per-method guards were the alternative and were worse three ways: nine of them answered in
/// three different shapes, nothing executed any of them on any lane, and the shape ListDevices
/// used (an empty list) told the enumerator "no endpoints" when the truth was "this host
/// cannot be asked", which is precisely the distinction the layer above pins a test on.
/// </summary>
[SupportedOSPlatform("macos")]
public sealed unsafe partial class CoreAudioHal : ICoreAudioHal
{
    private readonly List<Registration> _listeners = [];
    private readonly List<IoProc> _ioProcs = [];
    private bool _disposed;

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

    public CoreAudioStreamFormat ReadStreamFormat(uint deviceId)
    {
        AudioStreamBasicDescription asbd = default;
        uint size = (uint)sizeof(AudioStreamBasicDescription);
        AudioObjectPropertyAddress address = Address(Selector.StreamFormat, Scope.Input);
        int status = AudioObjectGetPropertyData(
            deviceId, &address, 0, IntPtr.Zero, &size, &asbd);
        if (status != NoError)
            throw new CoreAudioException($"reading the stream format of device {deviceId}", status);

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
        // Unpinned only once CoreAudio says the registration is GONE. A failed destroy leaves
        // the IOProc registered, and the pin is what its client data points at: freeing it
        // hands the next callback a dangling GCHandle, which is a process-level access
        // violation rather than a managed throw the trampoline could contain. Leaving the
        // registration listed instead gives Dispose one more attempt at process teardown, and
        // costs at worst one pinned callback on a device that is already failing.
        if (status != NoError)
            throw new CoreAudioException($"destroying the IOProc on device {live.DeviceId}", status);

        _ioProcs.Remove(live);
        live.Unpin();
    }

    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;

        // The last owner of whatever is still registered. Nothing here throws or checks a
        // status: the seam binds every release path to be throw-free, and there is no caller
        // left who could act on a failure.
        foreach (IoProc ioProc in _ioProcs)
        {
            AudioDeviceStop(ioProc.DeviceId, ioProc.ProcId);
            AudioDeviceDestroyIOProcID(ioProc.DeviceId, ioProc.ProcId);
            ioProc.Unpin();
        }
        _ioProcs.Clear();

        // Copied because Dispose() removes the registration from this list as it goes, which
        // is also why no Clear() follows: the list is empty by the time the loop ends.
        foreach (Registration registration in _listeners.ToList())
            registration.Dispose();
    }

    private IoProc Live(CoreAudioIoProcHandle ioProc, string call)
    {
        ArgumentNullException.ThrowIfNull(ioProc);
        if (ioProc is not IoProc live || !_ioProcs.Contains(live))
            throw new InvalidOperationException($"{call} was handed an IOProc handle this HAL does not hold");
        return live;
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

        // CoreAudio writes back what it actually filled, and the device list can shrink between
        // the sizing call and this one: unplug a mic in that window and the tail of the array
        // is untouched zeros, which the walk above would read as device id 0. That is
        // kAudioObjectUnknown, no device at all, and every property read against it fails.
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
        // A box with no default configured is not a failure, and neither is one where the
        // property is momentarily unreadable: 0 is kAudioObjectUnknown, which no device has, so
        // nothing gets flagged default. CaptureDevice.DefaultFor then falls back to the first
        // endpoint of the flow, which is the documented headless/RDP behaviour.
        return status == NoError ? deviceId : 0;
    }

    private static string ReadString(uint objectId, uint selector)
    {
        AudioObjectPropertyAddress address = Address(selector, Scope.Global);
        IntPtr cfString = IntPtr.Zero;
        uint size = (uint)IntPtr.Size;
        int status = AudioObjectGetPropertyData(objectId, &address, 0, IntPtr.Zero, &size, &cfString);
        // Status-checked like every other read here, rather than answered as "". This one
        // produces the device UID, which is the key a saved selection round-trips through, so
        // "" for a failed read means two unreadable devices collide on one id and a stored ""
        // reopens whichever the walk happens to reach first.
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
        // Status-checked rather than read as "no streams in this scope". A device CoreAudio
        // refuses to answer for is one the question could not be ASKED about, and answering 0
        // drops it from the walk silently: the picker then reports the mic as gone when the
        // truth is that the HAL refused. That is exactly the distinction ListDevices is
        // declared to keep, and the reason this class carries no per-method off-a-Mac guard.
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

            // AudioBufferList is a count followed by that many AudioBuffers. The array starts
            // at offset 8, not 4: AudioBuffer holds a pointer, so it is 8-aligned and the
            // count is followed by four bytes of padding. Capped at what the returned size can
            // actually hold, because the count is native data and walking past it reads off
            // the end of this array.
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
            // Nothing may cross back into CoreAudio: an exception unwinding through a native
            // frame tears the process down. The handler is a one-line "re-read the property"
            // in every caller, so what is swallowed is a state refresh that the next
            // notification, or the next read, redoes. Reporting it would need a channel that
            // does not exist on a CoreAudio notification thread.
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

            // The FIRST buffer only. A multi-channel non-interleaved stream puts each channel
            // in its own buffer, and CoreAudioFormat refuses that layout at open time, so
            // whatever is running here has all its channels interleaved in buffer zero.
            var buffer = (AudioBuffer*)(list + BufferListHeaderBytes);
            if (buffer->Data != IntPtr.Zero && buffer->DataByteSize > 0)
                ioProc.Callback(new ReadOnlySpan<byte>((void*)buffer->Data, (int)buffer->DataByteSize));
        }
        catch (Exception)
        {
            // Same rule as above, and stricter: this is the realtime IO thread. Letting an
            // exception unwind into CoreAudio tears the process down, so a handler that throws
            // costs one buffer of audio rather than the meeting.
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
        /// <param name="hal">The HAL that holds it.</param>
        /// <param name="objectId">The object being watched.</param>
        /// <param name="address">The property being watched.</param>
        /// <param name="handler">What to run on a change.</param>
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
            fixed (AudioObjectPropertyAddress* address = &_address)
            {
                // Status ignored: the only failure is a registration CoreAudio no longer has,
                // which is the state this call is trying to reach. The seam binds a
                // registration's release not to throw, and every caller reaches it from a
                // teardown path with nothing to fall back on.
                AudioObjectRemovePropertyListener(
                    _objectId, address, &OnPropertyChanged, GCHandle.ToIntPtr(Pin));
            }
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
        /// <param name="deviceId">The device it runs against.</param>
        /// <param name="callback">Where its buffers go.</param>
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

    // bufferSize is a CFIndex, which is a signed long: nint, not int, the way every other
    // CFIndex this backend declares is spelled (SecKeychainItems' CFDataGetLength and
    // CFDataCreate). A 32-bit argument leaves the top half of the register undefined by the
    // arm64 ABI, and the callee reads all 64 bits of it as the size of this buffer.
    [LibraryImport(CoreFoundationFramework)]
    private static partial byte CFStringGetCString(IntPtr theString, byte* buffer, nint bufferSize, uint encoding);

    [LibraryImport(CoreFoundationFramework)]
    private static partial void CFRelease(IntPtr cf);
}
