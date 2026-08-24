namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// A <see cref="FactAttribute"/> for a test that calls into WASAPI for real, skipped at
/// discovery anywhere else. The Windows twin of the macOS project's
/// <c>RequiresMacOSAttribute</c>, and here for the same reason: the TFM is
/// <c>net10.0-windows</c>, which BUILDS anywhere and would run these into a
/// <c>DllNotFoundException</c> off Windows rather than saying why.
///
/// CI runs the whole project on windows-latest, so nothing here is skipped there.
/// </summary>
internal sealed class RequiresWindowsAttribute : FactAttribute
{
    /// <param name="capability">What the test needs Windows FOR, folded into the skip reason:
    /// a skip list is only useful if it names the capability that went unexercised.</param>
    public RequiresWindowsAttribute(string capability)
    {
        if (!OperatingSystem.IsWindows())
            Skip = $"not running Windows, so this host cannot {capability}";
    }
}
