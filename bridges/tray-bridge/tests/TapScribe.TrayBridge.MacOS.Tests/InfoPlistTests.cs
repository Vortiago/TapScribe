using TapScribe.Bridge.MacOS;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// Tests over the macOS shell app's bundle manifest (#419). Its declarations are as much a
/// design decision as any code here (what the app looks like to the user, which Macs it will
/// start on, which TCC permissions it asks for) and they are edited by hand in a file no
/// compiler checks, so they get a regression gate.
///
/// Read from the BUILT bundle unless a test says otherwise, because the SDK rewrites some
/// keys on the way in and only the built values reach an operator's Mac.
/// </summary>
public class InfoPlistTests
{
    [Fact]
    public void CFBundleIdentifier_StaysTheKeychainAndLaunchServicesContract()
    {
        // Load-bearing far beyond naming. macOS scopes Keychain items to the bundle
        // identifier, so the operator's saved tap token is reachable under this string and
        // no other: changing it orphans every stored token exactly as renaming
        // windows-tray-bridge.json would orphan every saved Windows setting. LaunchServices
        // also dedupes by it, so a collision lets the OS launch the wrong app. A change here
        // needs a migration, not an edit.
        Assert.Equal("net.havso.tapscribe.traybridge", InfoPlist.Built.Text("CFBundleIdentifier"));
    }

    // The one claim that is about the tree rather than the product, hence the source
    // manifest: a version literal in a committed file is a hand-maintained copy that goes
    // stale between releases. tools/bump_version.py owns every statically declared version
    // and this bundle deliberately is not one. The built .app DOES carry both keys, stamped
    // from $(Version) through ApplicationDisplayVersion / ApplicationVersion, which is how
    // the release job's -p:Version= from the git tag reaches it.
    [Theory]
    [InlineData("CFBundleShortVersionString")]
    [InlineData("CFBundleVersion")]
    public void SourceInfoPlist_DeclaresNoVersionOfItsOwn(string key)
    {
        Assert.False(InfoPlist.Source.Declares(key));
    }

    // The other half of the same decision, and the half that is actually about what ships:
    // removing ApplicationDisplayVersion / ApplicationVersion from the csproj does not error,
    // it makes the SDK stamp its own default and silently ignore the release job's
    // -p:Version=. The absence test above stays green through exactly that regression.
    //
    // Presence is all this level can honestly claim. A build that was not given -p:Version=
    // legitimately reads the SDK's "1.0", and the macOS CI job is such a build, so asserting
    // any particular value here would fail on every run that is not a tagged release. The
    // value is checked against the tag at release time.
    [Theory]
    [InlineData("CFBundleShortVersionString")]
    [InlineData("CFBundleVersion")]
    public void BuiltInfoPlist_CarriesAVersion(string key)
    {
        Assert.True(InfoPlist.Built.Declares(key), $"{key} is not stamped into the bundle");
    }

    [Fact]
    public void InfoPlist_DeclaresTheAppMenuBarOnly()
    {
        // LSUIElement is what keeps the Bridge out of the Dock and out of Cmd-Tab. It is a
        // menu-bar app; a Dock icon would be a second, meaningless way to reach it.
        Assert.True(InfoPlist.Built.Flag("LSUIElement"));
    }

    [Fact]
    public void InfoPlist_ShipsTheSameMinimumSystemVersionTheFloorEnforces()
    {
        // Two gates for one rule, and they have to agree or one of them is decoration:
        // Launch Services refuses to open the bundle on an older Mac, and the floor catches
        // what it does not (a build run straight from the shell, or a copied .app). Asserted
        // against MacOSVersionFloor.Minimum rather than a retyped 14.4, and against the built
        // value because the SDK stamps this key from SupportedOSPlatformVersion: unset, it
        // would stamp the workload's own newest release and the .app would open on nothing.
        Assert.Equal(
            MacOSVersionFloor.Minimum,
            Version.Parse(InfoPlist.Built.Text("LSMinimumSystemVersion")));
    }

    // The two grants a meeting needs: the mic for this side of it, and Core Audio process
    // taps for what the other apps play (ADR-0020). TCC shows these strings verbatim in the
    // prompt and refuses to prompt at all without them, so a missing or blank one is both a
    // dead capture path and an operator reading an empty dialog.
    [Theory]
    [InlineData("NSMicrophoneUsageDescription")]
    [InlineData("NSAudioCaptureUsageDescription")]
    public void InfoPlist_ExplainsEveryAudioPermissionItAsksFor(string key)
    {
        Assert.True(InfoPlist.Built.Declares(key), $"{key} is not declared");
        Assert.False(string.IsNullOrWhiteSpace(InfoPlist.Built.Text(key)), $"{key} is blank");
    }

    [Fact]
    public void InfoPlist_AsksForNoScreenRecordingPermission()
    {
        // An absence, asserted rather than merely left out, because it is the promise
        // ADR-0020 makes: process taps capture system audio with no Screen Recording grant,
        // which is exactly why they were chosen over ScreenCaptureKit. A contributor
        // reaching for a screen API would quietly cost every operator that prompt and
        // Sequoia's recurring re-approval nag. Matched on the word rather than on today's
        // key names, since a future spelling would slip past a list, and on the built bundle
        // so an SDK-injected key counts too.
        string[] screenKeys =
            [.. InfoPlist.Built.Keys.Where(k => k.Contains("Screen", StringComparison.OrdinalIgnoreCase))];

        Assert.True(screenKeys.Length == 0, $"the bundle must ask for no Screen Recording: {string.Join(", ", screenKeys)}");
    }
}
