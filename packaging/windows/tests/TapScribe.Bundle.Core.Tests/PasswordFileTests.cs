using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// Tests for <see cref="PasswordFile"/> — reading the Recorder's <c>.auth-password</c>
/// for the tray's "Copy password" item. This is load-bearing: <c>start.sh</c> prints the
/// generated password to a terminal, and a Bundle has no terminal, so the tray menu is
/// the ONLY way an operator gets into the dashboard on first run. Every failure
/// therefore has to come back as an explainable result the tray can show in a balloon,
/// not as an exception that a menu handler would swallow.
/// </summary>
public class PasswordFileTests
{
    [Fact]
    public void Parse_TrimsTheTrailingNewlineTheRecorderWrites()
    {
        PasswordLookup result = PasswordFile.Parse("hunter2\n", "/data/.auth-password");

        Assert.Equal(PasswordStatus.Ok, result.Status);
        Assert.True(result.IsOk);
        Assert.Equal("hunter2", result.Password);
    }

    [Fact]
    public void Parse_TakesTheFirstNonBlankLine()
    {
        PasswordLookup result = PasswordFile.Parse("\r\n  hunter2  \r\ntrailing junk\r\n", "/data/.auth-password");

        Assert.Equal(PasswordStatus.Ok, result.Status);
        Assert.Equal("hunter2", result.Password);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("\r\n\r\n")]
    public void Parse_ReportsEmptyRatherThanHandingBackNothing(string contents)
    {
        // Copying "" to the clipboard and saying nothing would look like success and
        // leave the operator locked out with no idea why.
        PasswordLookup result = PasswordFile.Parse(contents, "/data/.auth-password");

        Assert.Equal(PasswordStatus.Empty, result.Status);
        Assert.False(result.IsOk);
        Assert.Null(result.Password);
        Assert.Contains("/data/.auth-password", result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Read_ReturnsMissing_WhenTheRecorderHasNotStartedYet()
    {
        string path = Path.Join(Path.GetTempPath(), "tapscribe-missing-" + Guid.NewGuid().ToString("n"));

        PasswordLookup result = PasswordFile.Read(path);

        Assert.Equal(PasswordStatus.Missing, result.Status);
        Assert.Null(result.Password);
        // The message has to name the path AND say why it might legitimately be absent
        // — the Recorder generates it on first boot, so "wait and retry" is the fix.
        Assert.Contains(path, result.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Read_ReturnsThePassword_WhenTheFileIsThere()
    {
        using var temp = new TempFile("s3cret\n");

        PasswordLookup result = PasswordFile.Read(temp.Path);

        Assert.Equal(PasswordStatus.Ok, result.Status);
        Assert.Equal("s3cret", result.Password);
    }

    [Fact]
    public void Read_ReturnsEmpty_ForAZeroLengthFile()
    {
        using var temp = new TempFile("");

        Assert.Equal(PasswordStatus.Empty, PasswordFile.Read(temp.Path).Status);
    }

    [Fact]
    public void Read_ReturnsUnreadable_RatherThanThrowing_WhenThePathIsADirectory()
    {
        // Stands in for the whole class of IO failures (locked file, ACL, bad path):
        // the menu handler must never see an exception.
        string dir = Path.Join(Path.GetTempPath(), "tapscribe-dir-" + Guid.NewGuid().ToString("n"));
        Directory.CreateDirectory(dir);
        try
        {
            PasswordLookup result = PasswordFile.Read(dir);

            Assert.Equal(PasswordStatus.Unreadable, result.Status);
            Assert.Null(result.Password);
            Assert.False(string.IsNullOrWhiteSpace(result.Message));
        }
        finally
        {
            Directory.Delete(dir);
        }
    }

    [Fact]
    public void Message_NeverLeaksThePasswordItself()
    {
        // The message is shown in a balloon / written to the log; the secret is not.
        PasswordLookup result = PasswordFile.Parse("hunter2\n", "/data/.auth-password");

        Assert.DoesNotContain("hunter2", result.Message, StringComparison.Ordinal);
    }

    private sealed class TempFile : IDisposable
    {
        public string Path { get; }

        public TempFile(string contents)
        {
            Path = System.IO.Path.Join(
                System.IO.Path.GetTempPath(), "tapscribe-pw-" + Guid.NewGuid().ToString("n"));
            File.WriteAllText(Path, contents);
        }

        public void Dispose()
        {
            if (File.Exists(Path))
                File.Delete(Path);
        }
    }
}
