namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// Tests for the shell's launch decision (#419). The bundle's LSMinimumSystemVersion already
/// stops Launch Services opening the app on an old Mac; this is the half that catches what
/// Launch Services does not, so it needs to actually refuse rather than log and carry on.
/// The ambient read and the output stream are parameters, so the refusal can be driven for a
/// macOS this box is not running.
/// </summary>
public class ProgramTests
{
    [Fact]
    public void Run_BelowTheFloor_RefusesAndSaysWhy()
    {
        var complaints = new StringWriter();

        int exit = Program.Run(new Version(14, 3), complaints);

        Assert.NotEqual(0, exit);
        Assert.Contains("14.4", complaints.ToString());
    }

    [Fact]
    public void Run_WithAVersionItCouldNotRead_AlsoRefuses()
    {
        var complaints = new StringWriter();

        int exit = Program.Run(null, complaints);

        Assert.NotEqual(0, exit);
        Assert.NotEmpty(complaints.ToString());
    }

    [Fact]
    public void Run_AtTheFloor_LaunchesSilently()
    {
        // Silence matters as much as the exit code: a menu-bar app has no console, so
        // anything written here on a healthy launch is noise nobody will ever read.
        var complaints = new StringWriter();

        int exit = Program.Run(new Version(14, 4), complaints);

        Assert.Equal(0, exit);
        Assert.Empty(complaints.ToString());
    }
}
