namespace TapScribe.Bundle.Windows.Tests;

/// <summary>
/// The command line <see cref="ShellTarget"/> hands CreateProcess.
///
/// Only the pure half is asserted: <see cref="ShellTarget.Open"/> launches the operator's
/// browser, which no test should do. What is worth pinning is that the whole target
/// survives into the command line — the previous implementation forwarded through
/// explorer.exe, which silently dropped a URL's query string and opened a folder window,
/// so every minted `/login?k=…` link missed the browser entirely.
///
/// Plain [Fact]: string formatting needs no Win32, so these run on every leg, unlike the
/// job-object tests beside them.
/// </summary>
public class ShellTargetTests
{
    [Fact]
    public void CommandLineFor_KeepsAQueryString_WhichIsTheWholeBug()
    {
        string line = ShellTarget.CommandLineFor("http://localhost:8001/login?k=abc-DEF_123");

        Assert.Contains("?k=abc-DEF_123", line, StringComparison.Ordinal);
        Assert.EndsWith("\"http://localhost:8001/login?k=abc-DEF_123\"", line, StringComparison.Ordinal);
    }

    [Fact]
    public void CommandLineFor_GoesThroughTheDocumentedUrlHandler()
    {
        // rundll32's FileProtocolHandler, not explorer.exe: the point of the change.
        Assert.StartsWith("rundll32.exe url.dll,FileProtocolHandler ", ShellTarget.CommandLineFor("http://x/"), StringComparison.Ordinal);
    }

    [Fact]
    public void CommandLineFor_QuotesTheTarget_SoASpacedLogPathStaysOneArgument()
    {
        // The log lives under %USERPROFILE%, which routinely has a space in it.
        string line = ShellTarget.CommandLineFor(@"C:\Users\Ola Nordmann\TapScribe\logs\recorder.log");

        Assert.EndsWith("\"C:\\Users\\Ola Nordmann\\TapScribe\\logs\\recorder.log\"", line, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void CommandLineFor_RejectsABlankTarget(string? blank)
    {
        Assert.ThrowsAny<ArgumentException>(() => ShellTarget.CommandLineFor(blank!));
    }

    [Fact]
    public void CommandLineFor_RefusesAQuote_RatherThanGuessingAtTheEscaping()
    {
        // Nothing TapScribe opens can contain one, so a quote means something upstream is
        // wrong; escaping it would hide that behind a URL the shell reads differently.
        Assert.Throws<ArgumentException>(() => ShellTarget.CommandLineFor("http://x/\"; calc.exe"));
    }
}
