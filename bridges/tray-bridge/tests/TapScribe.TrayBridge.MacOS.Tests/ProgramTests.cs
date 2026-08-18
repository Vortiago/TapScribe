namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// Tests for the shell's launch decision (#419). The bundle's LSMinimumSystemVersion already
/// stops Launch Services opening the app on an old Mac; this is the half that catches what
/// Launch Services does not, so it needs to actually refuse rather than log and carry on.
/// The ambient read, the output stream and the menu bar itself are all parameters, so the
/// refusal can be driven for a macOS this box is not running, and so the decision can be
/// driven at all: everything the real launch does is AppKit, which cannot be constructed
/// under a test host.
/// </summary>
public class ProgramTests
{
    [Fact]
    public void Run_BelowTheFloor_RefusesAndSaysWhy()
    {
        using var complaints = new StringWriter();

        int exit = Program.Run(new Version(14, 3), complaints, NeverLaunch);

        Assert.NotEqual(0, exit);
        Assert.Contains("14.4", complaints.ToString());
    }

    [Fact]
    public void Run_WithAVersionItCouldNotRead_AlsoRefuses()
    {
        using var complaints = new StringWriter();

        int exit = Program.Run(null, complaints, NeverLaunch);

        Assert.NotEqual(0, exit);
        Assert.NotEmpty(complaints.ToString());
    }

    [Fact]
    public void Run_AtTheFloor_LaunchesSilently()
    {
        // Silence matters as much as the exit code: a menu-bar app has no console, so
        // anything written here on a healthy launch is noise nobody will ever read.
        using var complaints = new StringWriter();

        int exit = Program.Run(new Version(14, 4), complaints, static () => { });

        Assert.Equal(0, exit);
        Assert.Empty(complaints.ToString());
    }

    [Fact]
    public void Run_AtTheFloor_StartsTheMenuBar()
    {
        using var complaints = new StringWriter();
        int launched = 0;

        Program.Run(new Version(15, 0), complaints, () => launched++);

        Assert.Equal(1, launched);
    }

    [Fact]
    public void Run_BelowTheFloor_StartsNoMenuBarAtAll()
    {
        // The ordering is the point, not just the exit code. Everything the launch touches
        // (CoreAudio's HAL, the status bar, the operator's Keychain) is exactly what an
        // unsupported Mac cannot be asked for, so the floor has to be decided before any of
        // it is reached rather than reported afterwards.
        using var complaints = new StringWriter();

        Program.Run(new Version(13, 6), complaints, NeverLaunch);
    }

    private static void NeverLaunch() =>
        Assert.Fail("the shell launched a menu bar on a Mac it had already refused");
}
