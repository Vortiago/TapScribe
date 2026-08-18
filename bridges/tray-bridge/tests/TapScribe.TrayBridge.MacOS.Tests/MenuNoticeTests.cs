using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// The one line a notice becomes in the menu (#419). The Mac shell surfaces
/// <see cref="ITrayView.ShowNotice"/> as a line under the status header rather than as a
/// system notification: a notification needs an authorization grant that an unsigned local
/// build routinely does not get, and a notice that silently never appears is worse than one
/// the operator finds where they already look. Slice 7 owns real failure signalling.
///
/// A menu item is a single line of a bounded width, and the messages arriving here are
/// exception text, so the squeeze into that shape is the decision worth pinning.
/// </summary>
public class MenuNoticeTests
{
    [Fact]
    public void Line_MarksAWarningApartFromAnInformation()
    {
        string warning = MenuNotice.Line("Meeting summary failed", "transcribe: no backend", NoticeKind.Warning);
        string information = MenuNotice.Line("Recording saved", "3 files", NoticeKind.Information);

        Assert.StartsWith("⚠", warning, StringComparison.Ordinal);
        Assert.DoesNotContain("⚠", information, StringComparison.Ordinal);
    }

    [Fact]
    public void Line_KeepsBothTheTitleAndTheMessage()
    {
        string line = MenuNotice.Line("Settings not saved", "Permission denied", NoticeKind.Warning);

        Assert.Contains("Settings not saved", line, StringComparison.Ordinal);
        Assert.Contains("Permission denied", line, StringComparison.Ordinal);
    }

    [Fact]
    public void Line_WithNothingToAdd_ShowsTheTitleAlone()
    {
        // Not every notice carries detail, and a trailing colon on a bare title reads as a
        // message that failed to render.
        string line = MenuNotice.Line("Summary ready", "", NoticeKind.Information);

        Assert.EndsWith("Summary ready", line, StringComparison.Ordinal);
    }

    [Fact]
    public void Line_FlattensAMultiLineMessage()
    {
        // The messages are exception text: an IOException from the settings save arrives with
        // its own line breaks, and a menu item renders them as one run of glyphs or truncates
        // at the first.
        string line = MenuNotice.Line("Settings not saved", "could not write\nAccess to the path is denied", NoticeKind.Warning);

        Assert.DoesNotContain('\n', line);
        Assert.Contains("could not write Access to the path is denied", line, StringComparison.Ordinal);
    }

    [Fact]
    public void Line_TooLongForAMenuItem_IsCutWithAnEllipsis()
    {
        string line = MenuNotice.Line("Meeting summary failed", new string('x', 400), NoticeKind.Warning);

        Assert.True(line.Length <= MenuNotice.MaxLength, $"a {line.Length}-character menu item is not one line");
        Assert.EndsWith("…", line, StringComparison.Ordinal);
    }

    [Fact]
    public void Line_ThatAlreadyFits_IsLeftWhole()
    {
        string line = MenuNotice.Line("Summary ready", "the meeting is written up", NoticeKind.Information);

        Assert.DoesNotContain("…", line, StringComparison.Ordinal);
    }
}
