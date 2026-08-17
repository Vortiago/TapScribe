using System.Reflection;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// What has to be true for this assembly to run on a machine that is not a Mac (#419).
///
/// It is plain net10.0 on purpose, so the ubuntu CI lane builds it, runs these tests, and
/// constructs <see cref="TrayStores"/> while it is at it. Everything in
/// <c>SecKeychainItems</c> is guarded by <c>OperatingSystem.IsMacOS()</c>, but a guard only
/// helps if it runs BEFORE the type that dlopens Security.framework initialises. That
/// ordering is a property of the IL rather than of the source, and getting it wrong fails
/// nondeterministically (the runtime is allowed to initialise early, not obliged to), so it
/// is asserted here rather than left to whichever lane happens to notice.
/// </summary>
public class SecKeychainItemsLoadingTests
{
    [Fact]
    public void TheFrameworkGlobals_AreNotBeforeFieldInit_SoTheNotOnAMacGuardsRunFirst()
    {
        // Globals holds the NativeLibrary.Load of two absolute /System/Library paths, which
        // throw on Linux. With beforefieldinit the runtime may initialise it at JIT time of
        // any method that MENTIONS a field, which is every method there and all of them
        // ahead of their own guard: the ubuntu lane would then fail in a type initialiser
        // instead of returning NotAvailable. The empty static constructor inside Globals is
        // the only thing clearing this flag, so deleting it as dead code is the regression
        // this catches, on every lane rather than only where the dlopen would fail.
        Type globals = typeof(SecKeychainItems).GetNestedType("Globals", BindingFlags.NonPublic)
            ?? throw new InvalidOperationException("SecKeychainItems no longer nests a Globals type");

        Assert.False(
            globals.Attributes.HasFlag(TypeAttributes.BeforeFieldInit),
            "Globals must keep its explicit static constructor, or the OperatingSystem.IsMacOS() guards can be outrun");
    }

    [RequiresNonMacOS("prove the not-on-a-Mac answer")]
    public void EveryCall_OffAMac_AnswersNotAvailable_RatherThanDlopeningSecurityFramework()
    {
        // The other half of the rule above, and the half that actually EXERCISES it: the
        // beforefieldinit pin says the guards CAN run first, this says they DO, and that each
        // of the four is guarded rather than three of them. Without it nothing on any lane
        // calls into this type off a Mac, so deleting a guard is invisible: the ubuntu lane
        // stays green because no test ever reached the dlopen it would have unleashed.
        SecKeychainItems keychain = TestKeychain.Item("off-a-mac");

        Assert.Equal(KeychainStatus.NotAvailable, keychain.Copy(out string? secret));
        Assert.Null(secret);
        Assert.Equal(KeychainStatus.NotAvailable, keychain.Add("unreachable"));
        Assert.Equal(KeychainStatus.NotAvailable, keychain.Update("unreachable"));
        Assert.Equal(KeychainStatus.NotAvailable, keychain.Delete());
    }
}
