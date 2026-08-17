using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// What macOS this Mac is running, as Apple's own "product version" (the 14.4.1 spelling,
/// not the Darwin kernel version). This is the ambient edge in front of the pure
/// <see cref="MacOSVersionFloor"/>: the policy takes a version as a parameter so it can be
/// tested against releases nobody here runs, and only this class touches the OS.
///
/// THE RULE FOR EVERY MAC CALL IN THIS PROJECT, stated here because this is the first code
/// that obeys it: reach the OS through P/Invoke, never through the managed ObjC bindings.
/// Constructing any NSObject-derived type under `dotnet test` throws inside
/// ObjCRuntime.Runtime, because the VSTest host never initialises the ObjC bridge, so a
/// bindings-based reader could carry no test at all. P/Invoke works in that same host,
/// which is why this asks sysctl rather than NSProcessInfo.
/// </summary>
public static partial class MacOSProductVersion
{
    private const string ProductVersionKey = "kern.osproductversion";

    /// <summary>The macOS this host is running, or <c>null</c> when the OS declines to say.
    /// </summary>
    public static Version? Current()
    {
        // Guarded, not merely documented: this assembly is plain net10.0 on purpose and
        // builds and runs on the ubuntu lane, where "libc" has no sysctlbyname and the
        // P/Invoke throws DllNotFound/EntryPointNotFound instead of honouring the
        // null-when-unreadable contract above. Not-a-Mac is exactly "the OS declines to
        // say", so it takes the same answer as an unreadable reading.
        if (!OperatingSystem.IsMacOS())
            return null;

        // sysctl's two-call shape: ask with no buffer to learn the length, then ask again
        // with one that size. The reading comes back NUL-terminated, which Parse trims.
        nuint length = 0;
        if (SysctlByName(ProductVersionKey, null, ref length, IntPtr.Zero, 0) != 0 || length == 0)
            return null;

        var buffer = new byte[length];
        if (SysctlByName(ProductVersionKey, buffer, ref length, IntPtr.Zero, 0) != 0)
            return null;

        return Parse(Encoding.UTF8.GetString(buffer));
    }

    /// <summary>The <paramref name="reading"/> the OS handed back, as a version, or
    /// <c>null</c> for anything it cannot make sense of. Tolerates the NUL terminator and
    /// padding a C string arrives with.</summary>
    // Unreadable is null rather than a sentinel version: the floor spells the two refusals
    // differently, and a sentinel would report a Mac whose version could not be read as a
    // Mac running an ancient one.
    internal static Version? Parse(string reading) =>
        Version.TryParse(reading.Trim().Trim('\0'), out Version? version) ? version : null;

    // On the declaration rather than on Current(), which is honestly all-platform: answering
    // "this host has no macOS version" off a Mac IS its contract, and the tests call it
    // there. CA1416 then walks the guard above and fails the build if a future caller
    // reaches this without one.
    [SupportedOSPlatform("macos")]
    [LibraryImport("libc", EntryPoint = "sysctlbyname", StringMarshalling = StringMarshalling.Utf8)]
    private static partial int SysctlByName(
        string name,
        [Out] byte[]? oldp,
        ref nuint oldlenp,
        IntPtr newp,
        nuint newlen);
}
