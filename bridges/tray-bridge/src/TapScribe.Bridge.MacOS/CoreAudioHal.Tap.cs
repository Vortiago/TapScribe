using System.Globalization;
using System.Runtime.InteropServices;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The system-audio half of the HAL: Core Audio process taps (#420).
///
/// Same bargain as the rest of <see cref="CoreAudioHal"/> and the same rule - no logic, only
/// native calls and status checks. Which endpoint to tap, what a rebind means and what order
/// to tear the three objects down in are all decided in
/// <see cref="MacOSSystemAudioCapture"/>, where a fake can drive them; the description handed
/// to CoreAudio is built by <see cref="CoreAudioAggregateDescription"/>, where a test can read
/// it. What is left here is the eleven native symbols and the twelfth thing, which is the one
/// awkward part of the whole backend:
///
/// <c>CATapDescription</c> is an ObjC class and is NOT bound by <c>Microsoft.macOS</c>, which
/// carries no CoreAudio namespace at all. It is reached through the ObjC runtime's own C entry
/// points rather than through a hand-written <c>NSObject</c> binding, because constructing any
/// NSObject-derived type under the test host faults inside <c>ObjCRuntime</c>: a binding would
/// make this file unreachable even by the symbol smoke test, and would push the facade upward
/// into the class that is supposed to be testable.
///
/// A separate file rather than a separate class, so the reflection half of
/// <c>CoreAudioUpstreamContractTests</c> - which counts the <c>[LibraryImport]</c>s on this
/// ONE type and makes the pinned table agree - still sees every native declaration the
/// backend makes.
/// </summary>
public sealed unsafe partial class CoreAudioHal
{
    private readonly List<ProcessTap> _taps = [];
    private readonly List<Aggregate> _aggregates = [];

    public CoreAudioTapHandle CreateProcessTap()
    {
        IntPtr description = NewGlobalStereoTapDescription();
        try
        {
            uint tapId = 0;
            int status = AudioHardwareCreateProcessTap(description, &tapId);
            if (status != NoError || tapId == 0)
                throw new CoreAudioException("creating a system-audio process tap", status);

            // The tap's own UID, read off the object rather than off the description: it is
            // what the aggregate lists the tap by, and CoreAudio is the one that assigned it.
            string uid = ReadString(tapId, Selector.TapUid);
            var tap = new ProcessTap(tapId, uid);
            lock (_registrations)
                _taps.Add(tap);
            return tap;
        }
        finally
        {
            // The description is a plain ObjC object this call alloc'd; the tap it produced
            // does not hold it. Released here rather than kept, because a rebind builds a
            // fresh one and there is nothing to reuse.
            ObjCMessageSend(description, ObjCSelector.Release);
        }
    }

    public CoreAudioStreamFormat ReadTapFormat(CoreAudioTapHandle tap)
    {
        ProcessTap live = LiveTap(tap, nameof(ReadTapFormat));
        return ReadStreamDescription(
            live.ObjectId, Selector.TapFormat, Scope.Global, $"reading the format of tap {live.ObjectId}");
    }

    public void DestroyProcessTap(CoreAudioTapHandle tap)
    {
        ProcessTap live = LiveTap(tap, nameof(DestroyProcessTap));
        int status = AudioHardwareDestroyProcessTap(live.ObjectId);
        // Listed until CoreAudio says it is gone, for the reason DestroyIoProc gives: a failed
        // destroy leaves the object registered, and forgetting it here would leave nothing able
        // to try again at Dispose.
        if (status != NoError)
            throw new CoreAudioException($"destroying the process tap {live.ObjectId}", status);

        lock (_registrations)
            _taps.Remove(live);
    }

    public CoreAudioAggregateHandle CreateAggregateDevice(string outputDeviceUid, CoreAudioTapHandle tap)
    {
        ArgumentNullException.ThrowIfNull(outputDeviceUid);
        ProcessTap live = LiveTap(tap, nameof(CreateAggregateDevice));

        // A fresh UID per aggregate. CoreAudio refuses a second one claiming a UID that is
        // still registered, and a process killed mid-meeting can leave its own behind until
        // the daemon reaps it, so a constant would make the NEXT launch the one that fails.
        string aggregateUid = "net.havso.tapscribe.tap." + Guid.NewGuid().ToString("n", CultureInfo.InvariantCulture);
        string plist = CoreAudioAggregateDescription.Plist(aggregateUid, outputDeviceUid, live.Uid);

        IntPtr description = ParsePropertyList(plist);
        try
        {
            uint deviceId = 0;
            int status = AudioHardwareCreateAggregateDevice(description, &deviceId);
            if (status != NoError || deviceId == 0)
                throw new CoreAudioException(
                    $"creating the aggregate device around tap {live.ObjectId}", status);

            var aggregate = new Aggregate(deviceId);
            lock (_registrations)
                _aggregates.Add(aggregate);
            return aggregate;
        }
        finally
        {
            CFRelease(description);
        }
    }

    public void DestroyAggregateDevice(CoreAudioAggregateHandle device)
    {
        Aggregate live = LiveAggregate(device, nameof(DestroyAggregateDevice));
        int status = AudioHardwareDestroyAggregateDevice(live.DeviceId);
        if (status != NoError)
            throw new CoreAudioException($"destroying the aggregate device {live.DeviceId}", status);

        lock (_registrations)
            _aggregates.Remove(live);
    }

    // The last owner of whatever tap or aggregate is still registered, called from Dispose.
    // Aggregates first: one lists a tap, and destroying the tap out from under it leaves the
    // device referring to an object that is gone. Nothing here checks a status - the seam binds
    // every release path to be throw-free, and there is no caller left who could act on one.
    private void ReleaseTapObjects()
    {
        foreach (Aggregate aggregate in Drain(_aggregates))
            AudioHardwareDestroyAggregateDevice(aggregate.DeviceId);

        foreach (ProcessTap tap in Drain(_taps))
            AudioHardwareDestroyProcessTap(tap.ObjectId);
    }

    private ProcessTap LiveTap(CoreAudioTapHandle tap, string call)
    {
        ArgumentNullException.ThrowIfNull(tap);
        if (tap is not ProcessTap live || !Holds(_taps, live))
            throw new InvalidOperationException($"{call} was handed a tap handle this HAL does not hold");
        return live;
    }

    private Aggregate LiveAggregate(CoreAudioAggregateHandle device, string call)
    {
        ArgumentNullException.ThrowIfNull(device);
        if (device is not Aggregate live || !Holds(_aggregates, live))
            throw new InvalidOperationException(
                $"{call} was handed an aggregate handle this HAL does not hold");
        return live;
    }

    /// <summary>One live process tap.</summary>
    /// <param name="objectId">Its <c>AudioObjectID</c>.</param>
    /// <param name="uid">Its <c>kAudioTapPropertyUID</c>, which is how an aggregate lists
    /// it.</param>
    private sealed class ProcessTap(uint objectId, string uid) : CoreAudioTapHandle
    {
        /// <summary>The tap's <c>AudioObjectID</c>.</summary>
        public uint ObjectId { get; } = objectId;

        /// <summary>The tap's UID, as CoreAudio assigned it.</summary>
        public string Uid { get; } = uid;
    }

    /// <summary>One live aggregate device wrapping a tap.</summary>
    /// <param name="deviceId">Its <c>AudioObjectID</c>.</param>
    private sealed class Aggregate(uint deviceId) : CoreAudioAggregateHandle
    {
        public override uint DeviceId { get; } = deviceId;
    }

    // ---- the two CoreFoundation / ObjC shapes this half needs -----------------------------

    // [[CATapDescription alloc] initStereoGlobalTapButExcludeProcesses:@[]] - everything the
    // Mac plays, in stereo, excluding nothing, left audible (the initialiser's own default
    // mute behaviour, which is why nothing here sets one).
    // Loaded once. The ObjC runtime only knows CATapDescription after the framework defining
    // it is mapped, and this is the one call that needs that guarantee; reloading per tap took
    // the dyld loader lock again for an image already in memory and leaked a refcount nothing
    // balances, since the handle was discarded.
    private static readonly IntPtr CoreAudioImage = NativeLibrary.Load(CoreAudioFramework);

    private static void EnsureCoreAudioLoaded() => _ = CoreAudioImage;

    private static IntPtr NewGlobalStereoTapDescription()
    {
        // Loaded explicitly rather than relied upon: the ObjC class lives in CoreAudio, and the
        // LibraryImports below bring the framework in lazily, so without this the very first
        // call could ask the runtime for a class from an image nothing has opened yet.
        EnsureCoreAudioLoaded();

        IntPtr tapDescription = objc_getClass("CATapDescription");
        if (tapDescription == IntPtr.Zero)
            throw new CoreAudioException("finding the CATapDescription class", ClassMissing);

        IntPtr allocated = ObjCMessageSend(tapDescription, ObjCSelector.Alloc);
        // An empty CFArray, which is an empty NSArray: the two are toll-free bridged, so this
        // is the exclude-nothing argument without a second runtime to talk to. Null callbacks
        // are what an empty collection wants - there is nothing to retain.
        IntPtr excludeNothing = CFArrayCreate(IntPtr.Zero, IntPtr.Zero, 0, IntPtr.Zero);
        try
        {
            IntPtr description = ObjCMessageSend(allocated, ObjCSelector.InitGlobalStereoTap, excludeNothing);
            return description != IntPtr.Zero
                ? description
                : throw new CoreAudioException("building a global stereo tap description", ClassMissing);
        }
        finally
        {
            CFRelease(excludeNothing);
        }
    }

    // The aggregate's description, as the CFDictionary CoreAudio wants. Parsed from a property
    // list rather than assembled call by call, so the keys and the value types are somewhere a
    // test can read them - see CoreAudioAggregateDescription.
    private static IntPtr ParsePropertyList(string plist)
    {
        byte[] utf8 = System.Text.Encoding.UTF8.GetBytes(plist);
        IntPtr data = CFDataCreate(IntPtr.Zero, utf8, utf8.Length);
        try
        {
            IntPtr error = IntPtr.Zero;
            uint format = 0;
            IntPtr parsed = CFPropertyListCreateWithData(IntPtr.Zero, data, 0, &format, &error);
            if (error != IntPtr.Zero)
                CFRelease(error);
            // The document is built from a template right here, so a parse failure is this
            // repo's bug rather than the platform's; it is still status-checked, because the
            // alternative is handing CoreAudio a null description and reading its complaint
            // about that instead.
            return parsed != IntPtr.Zero
                ? parsed
                : throw new CoreAudioException("parsing the aggregate device description", ClassMissing);
        }
        finally
        {
            CFRelease(data);
        }
    }

    private static IntPtr ObjCMessageSend(IntPtr receiver, string selector) =>
        objc_msgSend(receiver, sel_registerName(selector));

    private static IntPtr ObjCMessageSend(IntPtr receiver, string selector, IntPtr argument) =>
        objc_msgSend(receiver, sel_registerName(selector), argument);

    /// <summary>The ObjC selectors this half sends, spelled once. Apart from
    /// <see cref="Selector"/>, which holds CoreAudio's four-char property codes: these are
    /// messages to an object, not properties of one.</summary>
    private static class ObjCSelector
    {
        public const string Alloc = "alloc";
        public const string Release = "release";
        public const string InitGlobalStereoTap = "initStereoGlobalTapButExcludeProcesses:";
    }

    // Not an OSStatus CoreAudio produced: a missing ObjC class or an unparseable description is
    // this backend failing to reach the platform at all. kAudioHardwareUnspecifiedError ('what')
    // is the platform's own word for that, and using it keeps every failure out of here one
    // CoreAudioException with a real code rather than two shapes to filter on.
    private const int ClassMissing = 2003329396;

    private const string ObjCRuntimeLibrary = "/usr/lib/libobjc.A.dylib";

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioHardwareCreateProcessTap(IntPtr description, uint* tapId);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioHardwareDestroyProcessTap(uint tapId);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioHardwareCreateAggregateDevice(IntPtr description, uint* deviceId);

    [LibraryImport(CoreAudioFramework)]
    private static partial int AudioHardwareDestroyAggregateDevice(uint deviceId);

    [LibraryImport(ObjCRuntimeLibrary, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr objc_getClass(string name);

    [LibraryImport(ObjCRuntimeLibrary, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr sel_registerName(string name);

    // Two declarations of one entry point, because arm64 passes arguments in registers by the
    // CALL's signature: objc_msgSend is declared with the exact shape of the message being
    // sent, never as a variadic. Named apart so the source generator can emit both.
    [LibraryImport(ObjCRuntimeLibrary, EntryPoint = "objc_msgSend")]
    private static partial IntPtr objc_msgSend(IntPtr receiver, IntPtr selector);

    [LibraryImport(ObjCRuntimeLibrary, EntryPoint = "objc_msgSend")]
    private static partial IntPtr objc_msgSend(IntPtr receiver, IntPtr selector, IntPtr argument);

    [LibraryImport(CoreFoundationFramework)]
    private static partial IntPtr CFArrayCreate(IntPtr allocator, IntPtr values, nint count, IntPtr callBacks);

    [LibraryImport(CoreFoundationFramework)]
    private static partial IntPtr CFDataCreate(IntPtr allocator, [In] byte[] bytes, nint length);

    [LibraryImport(CoreFoundationFramework)]
    private static partial IntPtr CFPropertyListCreateWithData(
        IntPtr allocator, IntPtr data, uint options, uint* format, IntPtr* error);
}
