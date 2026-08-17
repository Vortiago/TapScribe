using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The real <see cref="IKeychainItems"/>: three SecItem calls against the login Keychain.
///
/// Through the Keychain C API rather than the managed Security bindings, for the reason
/// <see cref="MacOSProductVersion"/> states as the rule for every Mac call here, plus one
/// that is specific to this class: constructing any NSObject-derived type under the VSTest
/// host throws inside ObjCRuntime, so a bindings-based store could carry no test at all,
/// not even the round trip below. Everything here is P/Invocable and therefore testable.
///
/// The C API's shape is the whole of the awkwardness: an item is a CFDictionary of CFType
/// values keyed by CFString globals, so a call is "build a dictionary, make it, release
/// what you made". <see cref="CfScope"/> is what keeps the last part honest.
/// </summary>
/// <param name="service">kSecAttrService of the one item this instance speaks for.</param>
/// <param name="account">kSecAttrAccount of that item.</param>
internal sealed partial class SecKeychainItems(string service, string account) : IKeychainItems
{
    // Full framework paths: a bare "Security" is not reliably probed.
    private const string SecurityLibrary = "/System/Library/Frameworks/Security.framework/Security";
    private const string CoreFoundationLibrary =
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation";

    private const uint EncodingUtf8 = 0x08000100;  // kCFStringEncodingUTF8

    /// <summary>SecItemCopyMatching, asking for the password data of the one matching item.
    /// </summary>
    public int Copy(out string? secret)
    {
        secret = null;
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        IntPtr query = Query(
            scope,
            (Globals.ReturnData, Globals.True),
            (Globals.MatchLimit, Globals.MatchLimitOne));

        int status = SecItemCopyMatching(query, out IntPtr data);
        if (status != KeychainStatus.Success)
            return status;

        // A success with nothing attached is not a shape this query should be able to
        // produce, since it asks for kSecReturnData. It is guarded anyway because reading
        // the CFData below takes the process down rather than failing on a null, and an item
        // with no data (one another tool wrote) would be an odd way to lose the whole tray.
        if (data == IntPtr.Zero)
            return KeychainStatus.ItemNotFound;

        // Copy-rule: SecItemCopyMatching hands back a CFData this call owns. Handing it to
        // the scope rather than releasing it by hand is what makes that ownership look like
        // every other pointer here, and it releases on the throwing path too.
        secret = Utf8(scope.Keep(data));
        return KeychainStatus.Success;
    }

    /// <summary>SecItemAdd of one generic password. Refuses an existing item with
    /// errSecDuplicateItem; replacing is the caller's decision, not this one's.</summary>
    public int Add(string secret)
    {
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        return SecItemAdd(Query(scope, (Globals.ValueData, Secret(scope, secret))), IntPtr.Zero);
    }

    /// <summary>SecItemUpdate of one generic password. The query names the item and the
    /// second dictionary carries only what changes, which is why this cannot reuse Add's
    /// single-dictionary shape.</summary>
    public int Update(string secret)
    {
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        return SecItemUpdate(Query(scope), CfDictionary(scope, (Globals.ValueData, Secret(scope, secret))));
    }

    /// <summary>SecItemDelete of one generic password.</summary>
    public int Delete()
    {
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        return SecItemDelete(Query(scope));
    }

    // The dictionary that names this instance's item: this class, this service, this
    // account. Add's value and Copy's return-and-limit flags ride along as extra entries,
    // because in this API the query and the attributes to store are the same kind of thing.
    // Update is the exception and passes none, since what it changes goes in its own.
    [SupportedOSPlatform("macos")]
    private IntPtr Query(CfScope scope, params (IntPtr Key, IntPtr Value)[] extra) =>
        CfDictionary(
            scope,
            [
                (Globals.Class, Globals.ClassGenericPassword),
                (Globals.AttrService, CfString(scope, service)),
                (Globals.AttrAccount, CfString(scope, account)),
                .. extra,
            ]);

    [SupportedOSPlatform("macos")]
    private static IntPtr CfDictionary(CfScope scope, params (IntPtr Key, IntPtr Value)[] entries)
    {
        IntPtr[] keys = [.. entries.Select(entry => entry.Key)];
        IntPtr[] values = [.. entries.Select(entry => entry.Value)];
        // The kCFType callbacks make the dictionary retain what it holds for as long as it
        // holds it, which is what lets the scope release our own references on the way out.
        return scope.Keep(CFDictionaryCreate(
            IntPtr.Zero,
            keys,
            values,
            keys.Length,
            Globals.TypeDictionaryKeyCallBacks,
            Globals.TypeDictionaryValueCallBacks));
    }

    [SupportedOSPlatform("macos")]
    private static IntPtr Secret(CfScope scope, string secret)
    {
        byte[] bytes = Encoding.UTF8.GetBytes(secret);
        return scope.Keep(CFDataCreate(IntPtr.Zero, bytes, bytes.Length));
    }

    // Takes the scope like every other factory here. The one CF object this file created
    // without handing it over was the only leak still writable by forgetting a wrapper.
    [SupportedOSPlatform("macos")]
    private static IntPtr CfString(CfScope scope, string value) =>
        scope.Keep(CFStringCreateWithCString(IntPtr.Zero, value, EncodingUtf8));

    // The length-taking overload, not the NUL-terminated one: a CFData is a byte count and a
    // pointer, with no terminator promised, so reading it as a C string would run past the
    // secret into whatever follows it.
    [SupportedOSPlatform("macos")]
    private static string Utf8(IntPtr data)
    {
        nint length = CFDataGetLength(data);
        IntPtr bytes = CFDataGetBytePtr(data);
        return length <= 0 || bytes == IntPtr.Zero
            ? ""
            : Marshal.PtrToStringUTF8(bytes, (int)length);
    }

    /// <summary>Every CoreFoundation object this call owns, released together at the end of
    /// it: the ones it created, and the ones an API handed it under the copy rule.
    /// Create-and-forget is a leak in this API and there is no finalizer to catch it, so
    /// making the release structural is cheaper than remembering it.</summary>
    [SupportedOSPlatform("macos")]
    private sealed class CfScope : IDisposable
    {
        private readonly List<IntPtr> _created = [];

        public IntPtr Keep(IntPtr created)
        {
            if (created != IntPtr.Zero)
                _created.Add(created);
            return created;
        }

        public void Dispose()
        {
            foreach (IntPtr created in _created)
                CFRelease(created);
            _created.Clear();
        }
    }

    /// <summary>
    /// The framework globals these calls name. They are variables, not functions, so they
    /// are not P/Invocable: the export address is the address OF the variable, and a
    /// CFStringRef one has to be dereferenced to get the string. The two callback tables
    /// are structs, so for those the export address IS what the API wants.
    /// </summary>
    [SupportedOSPlatform("macos")]
    private static class Globals
    {
        // Explicit and empty on purpose: it suppresses beforefieldinit, so these dlopens
        // happen on first ACCESS of a field rather than at any earlier moment the runtime
        // finds convenient. Every method above refuses off a Mac before reaching one, and
        // that is only true if this type has not already initialised itself.
        // SecKeychainItemsLoadingTests pins it, since deleting an empty static constructor
        // looks like removing dead code and the damage lands on the ubuntu lane.
        static Globals()
        {
        }

        private static readonly IntPtr Security = NativeLibrary.Load(SecurityLibrary);
        private static readonly IntPtr CoreFoundation = NativeLibrary.Load(CoreFoundationLibrary);

        public static readonly IntPtr Class = Deref(Security, "kSecClass");
        public static readonly IntPtr ClassGenericPassword = Deref(Security, "kSecClassGenericPassword");
        public static readonly IntPtr AttrService = Deref(Security, "kSecAttrService");
        public static readonly IntPtr AttrAccount = Deref(Security, "kSecAttrAccount");
        public static readonly IntPtr ValueData = Deref(Security, "kSecValueData");
        public static readonly IntPtr ReturnData = Deref(Security, "kSecReturnData");
        public static readonly IntPtr MatchLimit = Deref(Security, "kSecMatchLimit");
        public static readonly IntPtr MatchLimitOne = Deref(Security, "kSecMatchLimitOne");

        public static readonly IntPtr True = Deref(CoreFoundation, "kCFBooleanTrue");
        public static readonly IntPtr TypeDictionaryKeyCallBacks =
            NativeLibrary.GetExport(CoreFoundation, "kCFTypeDictionaryKeyCallBacks");
        public static readonly IntPtr TypeDictionaryValueCallBacks =
            NativeLibrary.GetExport(CoreFoundation, "kCFTypeDictionaryValueCallBacks");

        private static IntPtr Deref(IntPtr library, string name) =>
            Marshal.ReadIntPtr(NativeLibrary.GetExport(library, name));
    }

    [SupportedOSPlatform("macos")]
    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemAdd(IntPtr attributes, IntPtr result);

    [SupportedOSPlatform("macos")]
    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemCopyMatching(IntPtr query, out IntPtr result);

    [SupportedOSPlatform("macos")]
    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemUpdate(IntPtr query, IntPtr attributesToUpdate);

    [SupportedOSPlatform("macos")]
    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemDelete(IntPtr query);

    [SupportedOSPlatform("macos")]
    [LibraryImport(CoreFoundationLibrary, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr CFStringCreateWithCString(IntPtr allocator, string value, uint encoding);

    [SupportedOSPlatform("macos")]
    [LibraryImport(CoreFoundationLibrary)]
    private static partial IntPtr CFDataCreate(IntPtr allocator, [In] byte[] bytes, nint length);

    [SupportedOSPlatform("macos")]
    [LibraryImport(CoreFoundationLibrary)]
    private static partial IntPtr CFDictionaryCreate(
        IntPtr allocator,
        [In] IntPtr[] keys,
        [In] IntPtr[] values,
        nint count,
        IntPtr keyCallBacks,
        IntPtr valueCallBacks);

    [SupportedOSPlatform("macos")]
    [LibraryImport(CoreFoundationLibrary)]
    private static partial nint CFDataGetLength(IntPtr data);

    [SupportedOSPlatform("macos")]
    [LibraryImport(CoreFoundationLibrary)]
    private static partial IntPtr CFDataGetBytePtr(IntPtr data);

    [SupportedOSPlatform("macos")]
    [LibraryImport(CoreFoundationLibrary)]
    private static partial void CFRelease(IntPtr cf);
}
