using System.Runtime.InteropServices;
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
internal sealed partial class SecKeychainItems : IKeychainItems
{
    // Full framework paths: a bare "Security" is not reliably probed.
    private const string SecurityLibrary = "/System/Library/Frameworks/Security.framework/Security";
    private const string CoreFoundationLibrary =
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation";

    private const uint EncodingUtf8 = 0x08000100;  // kCFStringEncodingUTF8

    /// <summary>SecItemCopyMatching, asking for the password data of the one matching item.
    /// </summary>
    public int Copy(string service, string account, out string? secret)
    {
        secret = null;
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        IntPtr query = Query(
            scope,
            service,
            account,
            (Globals.ReturnData, Globals.True),
            (Globals.MatchLimit, Globals.MatchLimitOne));

        int status = SecItemCopyMatching(query, out IntPtr data);
        if (status != KeychainStatus.Success)
            return status;

        // A success with nothing attached is not a shape this query should be able to
        // produce, since it asks for kSecReturnData. It is guarded anyway because the two
        // calls below take the process down rather than failing on a null, and an item with
        // no data (one another tool wrote) would be an odd way to lose the whole tray.
        if (data == IntPtr.Zero)
            return KeychainStatus.ItemNotFound;

        // Copy-rule: SecItemCopyMatching hands back an owned CFData, and it is not the
        // scope's because it was not created here.
        try
        {
            secret = Utf8(data);
        }
        finally
        {
            CFRelease(data);
        }
        return KeychainStatus.Success;
    }

    /// <summary>SecItemAdd of one generic password. Refuses an existing item with
    /// errSecDuplicateItem; replacing is the caller's decision, not this one's.</summary>
    public int Add(string service, string account, string secret)
    {
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        byte[] bytes = Encoding.UTF8.GetBytes(secret);
        IntPtr attributes = Query(
            scope,
            service,
            account,
            (Globals.ValueData, scope.Keep(CFDataCreate(IntPtr.Zero, bytes, bytes.Length))));

        return SecItemAdd(attributes, IntPtr.Zero);
    }

    /// <summary>SecItemDelete of one generic password.</summary>
    public int Delete(string service, string account)
    {
        if (!OperatingSystem.IsMacOS())
            return KeychainStatus.NotAvailable;

        using var scope = new CfScope();
        return SecItemDelete(Query(scope, service, account));
    }

    // The dictionary all three calls share: this class of item, this service, this account.
    // Add's value and Copy's return-and-limit flags ride along as extra entries, because in
    // this API the query and the attributes to store are the same kind of thing.
    private static IntPtr Query(
        CfScope scope, string service, string account, params (IntPtr Key, IntPtr Value)[] extra)
    {
        (IntPtr Key, IntPtr Value)[] entries =
        [
            (Globals.Class, Globals.ClassGenericPassword),
            (Globals.AttrService, scope.Keep(CFString(service))),
            (Globals.AttrAccount, scope.Keep(CFString(account))),
            .. extra,
        ];

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

    private static IntPtr CFString(string value) =>
        CFStringCreateWithCString(IntPtr.Zero, value, EncodingUtf8);

    private static string Utf8(IntPtr data)
    {
        nint length = CFDataGetLength(data);
        IntPtr bytes = CFDataGetBytePtr(data);
        if (length <= 0 || bytes == IntPtr.Zero)
            return "";

        byte[] buffer = new byte[length];
        Marshal.Copy(bytes, buffer, 0, (int)length);
        return Encoding.UTF8.GetString(buffer);
    }

    /// <summary>Every CoreFoundation object created during one call, released together at
    /// the end of it. Create-and-forget is a leak in this API and there is no finalizer to
    /// catch it, so making the release structural is cheaper than remembering it.</summary>
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
    private static class Globals
    {
        // Explicit and empty on purpose: it suppresses beforefieldinit, so these dlopens
        // happen on first ACCESS of a field rather than at any earlier moment the runtime
        // finds convenient. Every method above refuses off a Mac before reaching one, and
        // that is only true if this type has not already initialised itself.
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

    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemAdd(IntPtr attributes, IntPtr result);

    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemCopyMatching(IntPtr query, out IntPtr result);

    [LibraryImport(SecurityLibrary)]
    private static partial int SecItemDelete(IntPtr query);

    [LibraryImport(CoreFoundationLibrary, StringMarshalling = StringMarshalling.Utf8)]
    private static partial IntPtr CFStringCreateWithCString(IntPtr allocator, string value, uint encoding);

    [LibraryImport(CoreFoundationLibrary)]
    private static partial IntPtr CFDataCreate(IntPtr allocator, [In] byte[] bytes, nint length);

    [LibraryImport(CoreFoundationLibrary)]
    private static partial IntPtr CFDictionaryCreate(
        IntPtr allocator,
        [In] IntPtr[] keys,
        [In] IntPtr[] values,
        nint count,
        IntPtr keyCallBacks,
        IntPtr valueCallBacks);

    [LibraryImport(CoreFoundationLibrary)]
    private static partial nint CFDataGetLength(IntPtr data);

    [LibraryImport(CoreFoundationLibrary)]
    private static partial IntPtr CFDataGetBytePtr(IntPtr data);

    [LibraryImport(CoreFoundationLibrary)]
    private static partial void CFRelease(IntPtr cf);
}
