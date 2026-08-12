using TapScribe.Bridge.MacOS;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// Tests over the macOS shell app's <c>Info.plist</c> (#419). The bundle's declarations are
/// as much a design decision as any code here - what the app looks like to the user
/// (menu-bar only), which Macs it will start on, and which TCC permissions it asks for -
/// and they are edited by hand in a file no compiler checks, so they get a regression gate.
/// </summary>
public class InfoPlistTests
{
    [Fact]
    public void InfoPlist_DeclaresTheAppMenuBarOnly()
    {
        // LSUIElement is what keeps the Bridge out of the Dock and out of Cmd-Tab. It is a
        // menu-bar app; a Dock icon would be a second, meaningless way to reach it.
        Assert.True(InfoPlist.Flag("LSUIElement"));
    }

    [Fact]
    public void InfoPlist_DeclaresTheSameMinimumSystemVersionAsTheFloor()
    {
        // Two gates for one rule, and they have to agree or one of them is decoration:
        // Launch Services refuses to open the bundle on an older Mac, and the floor catches
        // what it does not (a build run straight from the shell, or a bundle whose plist
        // was edited without the code following).
        Assert.Equal(
            MacOSVersionFloor.Minimum,
            Version.Parse(InfoPlist.Entries["LSMinimumSystemVersion"].Value));
    }

    [Fact]
    public void ShellProject_StampsTheBundleWithTheMinimumSystemVersionItDeclares()
    {
        // The plist entry above is the readable declaration, but it is not what ships: the
        // SDK writes LSMinimumSystemVersion into the built bundle from
        // SupportedOSPlatformVersion, overwriting the source. Unset, it stamps the macos
        // workload's own newest release, so the .app would refuse to open on every Mac this
        // Bridge targets. Pinning them to each other is what stops the plist becoming a
        // comment.
        Assert.Equal(
            InfoPlist.Entries["LSMinimumSystemVersion"].Value,
            ShellProject.Property("SupportedOSPlatformVersion"));
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
        Assert.True(InfoPlist.Entries.ContainsKey(key), $"{key} is not declared");
        Assert.False(string.IsNullOrWhiteSpace(InfoPlist.Entries[key].Value), $"{key} is blank");
    }

    [Fact]
    public void InfoPlist_AsksForNoScreenRecordingPermission()
    {
        // An absence, asserted rather than merely left out, because it is the promise
        // ADR-0020 makes: process taps capture system audio with no Screen Recording grant,
        // which is exactly why they were chosen over ScreenCaptureKit. A contributor
        // reaching for a screen API would quietly cost every operator that prompt and
        // Sequoia's recurring re-approval nag. Matched on the word rather than on today's
        // key names, since a future spelling would slip past a list.
        string[] screenKeys =
            [.. InfoPlist.Entries.Keys.Where(k => k.Contains("Screen", StringComparison.OrdinalIgnoreCase))];

        Assert.True(screenKeys.Length == 0, $"the bundle must ask for no Screen Recording: {string.Join(", ", screenKeys)}");
    }
}
