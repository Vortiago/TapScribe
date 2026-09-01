using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// Tests for <see cref="LogRotation"/> — the pure size+count decision behind the
/// host role's rotating log. No filesystem here at all: the input is a list of
/// (name, size) and the output is "roll to this archive name, delete these files",
/// which the host role's writer then performs. That split is what makes the policy
/// testable on Linux while the file handles stay in the Windows shell.
/// </summary>
public class LogRotationTests
{
    private const string Active = "recorder.log";
    private static readonly DateTimeOffset Now = new(2026, 7, 19, 12, 13, 14, TimeSpan.Zero);
    private static readonly LogRotationPolicy Policy = new(MaxBytes: 1000, KeepArchives: 3);

    private static LogRotationPlan Plan(params LogFileInfo[] files) =>
        LogRotation.Plan(Active, files, Now, Policy);

    [Fact]
    public void Plan_DoesNothing_WhenTheActiveLogIsUnderTheLimit()
    {
        LogRotationPlan plan = Plan(new LogFileInfo(Active, 999));

        Assert.False(plan.ShouldRoll);
        Assert.Null(plan.ArchiveName);
        Assert.Empty(plan.Delete);
    }

    [Fact]
    public void Plan_DoesNothing_OnAFirstRunWithNoLogAtAll()
    {
        LogRotationPlan plan = Plan();

        Assert.False(plan.ShouldRoll);
        Assert.Empty(plan.Delete);
    }

    [Fact]
    public void Plan_Rolls_WhenTheActiveLogReachesTheLimit()
    {
        LogRotationPlan plan = Plan(new LogFileInfo(Active, 1000));

        Assert.True(plan.ShouldRoll);
        // A sortable UTC stamp, so ordinal name order IS chronological order —
        // which is what lets the delete decision be a pure sort over names.
        Assert.Equal("recorder-20260719-121314.log", plan.ArchiveName);
        Assert.Empty(plan.Delete);
    }

    [Fact]
    public void Plan_KeepsTheArchiveNameUnique_WhenTwoRollsLandInTheSameSecond()
    {
        LogRotationPlan plan = Plan(
            new LogFileInfo(Active, 5000),
            new LogFileInfo("recorder-20260719-121314.log", 5000));

        Assert.Equal("recorder-20260719-121314-1.log", plan.ArchiveName);
    }

    [Fact]
    public void Plan_DeletesTheOldestArchives_CountingTheOneAboutToBeCreated()
    {
        // KeepArchives=3 and three archives already exist: after the roll there are
        // four, so the oldest goes.
        LogRotationPlan plan = Plan(
            new LogFileInfo(Active, 4096),
            new LogFileInfo("recorder-20260719-100000.log", 10),
            new LogFileInfo("recorder-20260719-110000.log", 10),
            new LogFileInfo("recorder-20260719-120000.log", 10));

        Assert.True(plan.ShouldRoll);
        Assert.Equal(new[] { "recorder-20260719-100000.log" }, plan.Delete);
    }

    [Fact]
    public void Plan_DeletesEveryArchiveOverBudget_OldestFirst()
    {
        LogRotationPlan plan = Plan(
            new LogFileInfo(Active, 4096),
            new LogFileInfo("recorder-20260719-090000.log", 10),
            new LogFileInfo("recorder-20260719-100000.log", 10),
            new LogFileInfo("recorder-20260719-110000.log", 10),
            new LogFileInfo("recorder-20260719-120000.log", 10));

        Assert.Equal(
            new[] { "recorder-20260719-090000.log", "recorder-20260719-100000.log" },
            plan.Delete);
    }

    [Fact]
    public void Plan_PrunesOverBudgetArchives_EvenWhenNotRolling()
    {
        // The operator tightened the policy (or a previous run crashed mid-prune):
        // the surplus still has to go, and it must not wait for the next roll.
        LogRotationPlan plan = Plan(
            new LogFileInfo(Active, 1),
            new LogFileInfo("recorder-20260719-090000.log", 10),
            new LogFileInfo("recorder-20260719-100000.log", 10),
            new LogFileInfo("recorder-20260719-110000.log", 10),
            new LogFileInfo("recorder-20260719-120000.log", 10));

        Assert.False(plan.ShouldRoll);
        Assert.Equal(new[] { "recorder-20260719-090000.log" }, plan.Delete);
    }

    [Fact]
    public void Plan_NeverDeletesTheActiveLog()
    {
        LogRotationPlan plan = Plan(
            new LogFileInfo(Active, 999_999),
            new LogFileInfo("recorder-20260719-090000.log", 10),
            new LogFileInfo("recorder-20260719-100000.log", 10),
            new LogFileInfo("recorder-20260719-110000.log", 10),
            new LogFileInfo("recorder-20260719-120000.log", 10));

        Assert.DoesNotContain(Active, plan.Delete);
    }

    [Fact]
    public void Plan_IgnoresUnrelatedFilesInTheLogDirectory()
    {
        // The log dir is the operator's; a stray file must never be deleted just for
        // sitting next to our logs.
        LogRotationPlan plan = Plan(
            new LogFileInfo(Active, 4096),
            new LogFileInfo("notes.txt", 10),
            new LogFileInfo("recorder.log.old", 10),
            new LogFileInfo("other-20260719-090000.log", 10),
            new LogFileInfo("recorder-nope.log", 10));

        Assert.Empty(plan.Delete);
    }

    [Fact]
    public void Plan_TreatsArchiveNamesCaseInsensitively_BecauseWindowsDoes()
    {
        LogRotationPlan plan = Plan(
            new LogFileInfo("RECORDER.LOG", 4096),
            new LogFileInfo("Recorder-20260719-090000.LOG", 10),
            new LogFileInfo("recorder-20260719-100000.log", 10),
            new LogFileInfo("recorder-20260719-110000.log", 10));

        Assert.True(plan.ShouldRoll);
        Assert.Equal(new[] { "Recorder-20260719-090000.LOG" }, plan.Delete);
    }

    [Fact]
    public void DefaultPolicy_IsBoundedInBothSizeAndCount()
    {
        Assert.True(LogRotationPolicy.Default.MaxBytes > 0);
        Assert.True(LogRotationPolicy.Default.KeepArchives >= 1);
        // A Recorder that runs for weeks must have a bounded worst case on disk.
        Assert.True(LogRotationPolicy.Default.MaxBytes * (LogRotationPolicy.Default.KeepArchives + 1) < 100L * 1024 * 1024);
    }

    [Theory]
    [InlineData(0, 3)]
    [InlineData(-1, 3)]
    [InlineData(1000, 0)]
    [InlineData(1000, -1)]
    public void Policy_RejectsAnUnboundedOrSelfErasingConfiguration(long maxBytes, int keep)
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => new LogRotationPolicy(maxBytes, keep));
    }
}
