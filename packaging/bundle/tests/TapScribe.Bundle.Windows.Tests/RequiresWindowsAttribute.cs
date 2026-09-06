namespace TapScribe.Bundle.Windows.Tests;

/// <summary>
/// A <see cref="FactAttribute"/> for a test that calls into the Win32 job-object API for
/// real, skipped at discovery anywhere else. A copy of the tray Bridge's attribute of the
/// same name rather than a shared one: the two trees are separate solutions with no
/// project between them, and a dependency edge from packaging to bridges for eight lines
/// would be the worse trade.
/// </summary>
internal sealed class RequiresWindowsAttribute : FactAttribute
{
    /// <summary>Skip at discovery unless the host is Windows.</summary>
    /// <param name="capability">What the test needs Windows FOR, folded into the skip
    /// reason: a skip list is only useful if it names the capability that went
    /// unexercised.</param>
    public RequiresWindowsAttribute(string capability)
    {
        if (!OperatingSystem.IsWindows())
            Skip = $"not running Windows, so this host cannot {capability}";
    }
}
