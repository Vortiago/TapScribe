namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// The macOS watchdog's argv, both directions (ADR-0024).
///
/// Worth its own tests because the failure is not a crash. This argv decides which process
/// group gets a SIGKILL, in a process whose entire job is to send one — so a loose parse
/// that turned junk into a plausible id would kill something nobody asked it to, on a
/// machine where nothing is left running to notice.
/// </summary>
public class ReapRequestTests
{
    [Fact]
    public void TheWatchdogArgvRoundTripsBackToTheSameRequest()
    {
        // The two halves are written apart — one builds the command line, the other reads
        // it in a second process — so this is the only place they meet.
        var request = new ReapRequest(TrayPid: 4321, GroupId: 4321);

        BundleProcess command = RecorderCommand.Watchdog("/Applications/TapScribe.app/Contents/MacOS/TapScribe", request);

        Assert.Equal(request, ReapRequest.Parse(command.Arguments));
    }

    [Fact]
    public void TheWatchdogIsTheTraysOwnBinary()
    {
        // One thing to sign, one thing to install, and nothing that can go missing on its
        // own — which a second shipped helper could.
        const string tray = "/Applications/TapScribe.app/Contents/MacOS/TapScribe";

        BundleProcess command = RecorderCommand.Watchdog(tray, new ReapRequest(1, 2));

        Assert.Equal(tray, command.Executable);
    }

    [Fact]
    public void TheWatchdogInheritsNoRecorderEnvironment()
    {
        // It reads no config and resolves no paths. A TAPSCRIBE_BASE_DIR on it would make a
        // bare signal-sender look like a Recorder to anything reading the process table.
        BundleProcess command = RecorderCommand.Watchdog("/tray", new ReapRequest(1, 2));

        Assert.Empty(command.Environment);
    }

    [Fact]
    public void AnOrdinaryLaunchIsNotAWatchdogInvocation()
    {
        // The overwhelmingly common case: the tray was started by the operator.
        Assert.Null(ReapRequest.Parse([]));
        Assert.Null(ReapRequest.Parse(["--some-other-flag"]));
    }

    [Theory]
    [InlineData("--reap-group", "123")]                   // one id short
    [InlineData("--reap-group", "123", "456", "789")]     // one too many
    [InlineData("--reap-group", "notanumber", "456")]
    [InlineData("--reap-group", "123", "")]
    [InlineData("--reap-group", "123", " 456")]
    [InlineData("--reap-group", "123", "4_5_6")]
    public void AMalformedInvocationIsRefusedRatherThanGuessedAt(params string[] args)
    {
        Assert.Null(ReapRequest.Parse(args));
    }

    [Theory]
    [InlineData("0")]
    [InlineData("-1")]
    public void TheKillpgArgumentsThatMeanSomethingElseAreRefused(string id)
    {
        // These are not merely invalid, they are DANGEROUS: killpg(0, sig) signals the
        // caller's own group and killpg(-1, sig) signals every process the user may signal.
        // Reaching either through a mis-parse would take the operator's session down.
        Assert.Null(ReapRequest.Parse(["--reap-group", "123", id]));
        Assert.Null(ReapRequest.Parse(["--reap-group", id, "123"]));
    }
}
