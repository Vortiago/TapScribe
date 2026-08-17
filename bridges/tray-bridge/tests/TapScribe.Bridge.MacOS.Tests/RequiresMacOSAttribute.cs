namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>A <see cref="FactAttribute"/> that skips the test at discovery unless the host
/// is actually running macOS. Almost everything in this assembly is pure policy and runs
/// anywhere; this is for the handful of tests that ask the running OS a question and so can
/// only mean something on a Mac.
///
/// Sets the standard <c>Skip</c> property, which every runner honours (xunit v2's
/// dynamic-skip token is NOT recognised by <c>dotnet test</c>), so the ubuntu lane skips
/// these cleanly instead of failing.
///
/// Gates on <c>OperatingSystem.IsMacOS()</c>, the same predicate the guards under test use.
/// The two are not interchangeable (Mac Catalyst is where they part), and a test gated on
/// one while asserting about the other is a test that can run against a library that
/// refuses, or skip while the guard it covers is live.</summary>
internal sealed class RequiresMacOSAttribute : FactAttribute
{
    /// <summary>Marks the test as needing a real Mac, skipping it at discovery elsewhere.
    /// </summary>
    /// <param name="capability">What the test needs a Mac FOR, folded into the skip reason.
    /// The ubuntu lane's skip list is the only signal that a piece of P/Invoke went
    /// unexercised there, so it has to name the capability rather than whichever test
    /// happened to be written first.</param>
    public RequiresMacOSAttribute(string capability)
    {
        if (!OperatingSystem.IsMacOS())
            Skip = $"not running macOS, so this host cannot {capability}";
    }
}

/// <summary>The mirror of <see cref="RequiresMacOSAttribute"/>, for the other half of the
/// OS line: a test that asserts what this assembly does when it is NOT on a Mac. The
/// off-a-Mac behaviour is a real contract rather than an accident (the ubuntu lane builds and
/// runs this assembly, so every OS call has to answer rather than throw), and it can only be
/// observed where that is actually true.
///
/// Both halves exist because xunit v2's dynamic-skip token is not recognised by
/// <c>dotnet test</c>: the decision has to be made at discovery, through <c>Skip</c>, so it
/// takes an attribute either way rather than an early return inside the test, which would
/// read as a pass on the lane that skipped the assertions.</summary>
internal sealed class RequiresNonMacOSAttribute : FactAttribute
{
    /// <summary>Marks the test as needing a host that is NOT a Mac, skipping it at discovery
    /// on one.</summary>
    /// <param name="contract">What the test pins about being off a Mac, folded into the skip
    /// reason so the macos lane's skip list says which contract went unexercised there.</param>
    public RequiresNonMacOSAttribute(string contract)
    {
        if (OperatingSystem.IsMacOS())
            Skip = $"running macOS, so this host cannot {contract}";
    }
}
