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
}
