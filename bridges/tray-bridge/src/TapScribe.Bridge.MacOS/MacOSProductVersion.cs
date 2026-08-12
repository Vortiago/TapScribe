using System.Runtime.InteropServices;
using System.Text;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// What macOS this Mac is running, as Apple's own "product version" (the 14.4.1 spelling,
/// not the Darwin kernel version). This is the ambient edge in front of the pure
/// <see cref="MacOSVersionFloor"/>: the policy takes a version as a parameter so it can be
/// tested against releases nobody here runs, and only this class touches the OS.
///
/// The read is a libc P/Invoke rather than NSProcessInfo on purpose. The managed ObjC
/// bindings are unusable under `dotnet test` - constructing any NSObject-derived type
/// throws inside ObjCRuntime.Runtime, because the VSTest host never initialises the ObjC
/// bridge - so a bindings-based reader could not be covered by a test at all.
/// </summary>
public static partial class MacOSProductVersion
{
    private const string ProductVersionKey = "kern.osproductversion";

    /// <summary>The macOS this host is running, or a version below every real macOS when
    /// the OS declines to say (see <see cref="Parse"/>).</summary>
    public static Version Current()
    {
        // sysctl's two-call shape: ask with no buffer to learn the length, then ask again
        // with one that size. The reading comes back NUL-terminated, which Parse trims.
        nuint length = 0;
        if (SysctlByName(ProductVersionKey, null, ref length, IntPtr.Zero, 0) != 0 || length == 0)
            return Unreadable;

        var buffer = new byte[length];
        if (SysctlByName(ProductVersionKey, buffer, ref length, IntPtr.Zero, 0) != 0)
            return Unreadable;

        return Parse(Encoding.UTF8.GetString(buffer));
    }

    /// <summary>The <paramref name="reading"/> the OS handed back, as a version. Tolerates
    /// the NUL terminator and padding a C string arrives with, and answers
    /// <see cref="Unreadable"/> for anything it cannot make sense of.</summary>
    public static Version Parse(string reading) =>
        Version.TryParse(reading.Trim().Trim('\0'), out Version? version) ? version : Unreadable;

    // A Mac that will not say what it runs is not one this Bridge supports, so the
    // unreadable case is spelled as a version below every real macOS: the floor then
    // refuses it through the same path as a genuinely old Mac, and nothing on the launch
    // path has to handle an exception.
    private static Version Unreadable { get; } = new(0, 0);

    [LibraryImport("libc", EntryPoint = "sysctlbyname", StringMarshalling = StringMarshalling.Utf8)]
    private static partial int SysctlByName(
        string name,
        [Out] byte[]? oldp,
        ref nuint oldlenp,
        IntPtr newp,
        nuint newlen);
}
