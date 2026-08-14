using System.Runtime.InteropServices;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>A <see cref="FactAttribute"/> that skips the test at discovery unless the host
/// is actually running macOS. Almost everything in this assembly is pure policy and runs
/// anywhere; this is for the handful of tests that ask the running OS a question and so can
/// only mean something on a Mac.
///
/// Sets the standard <c>Skip</c> property, which every runner honours (xunit v2's
/// dynamic-skip token is NOT recognised by <c>dotnet test</c>), so the ubuntu lane skips
/// these cleanly instead of failing.</summary>
internal sealed class RequiresMacOSAttribute : FactAttribute
{
    /// <param name="capability">What the test needs a Mac FOR, folded into the skip reason.
    /// The ubuntu lane's skip list is the only signal that a piece of P/Invoke went
    /// unexercised there, so it has to name the capability rather than whichever test
    /// happened to be written first.</param>
    public RequiresMacOSAttribute(string capability = "answer for itself")
    {
        if (!RuntimeInformation.IsOSPlatform(OSPlatform.OSX))
            Skip = $"not running macOS, so this host cannot {capability}";
    }
}
