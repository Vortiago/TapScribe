using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The Past-meetings (#168) record: a readable submenu line derived from the
/// start time (+ optional label). Deterministic (fixed pattern, invariant
/// culture) so it's pinned in Core rather than the WinForms shell.
/// </summary>
public class MeetingRecordTests
{
    private static MeetingRecord At(int hour, int minute, string? label = null) => new()
    {
        SessionId = "2026-06-24T10-00-00",
        StartedAt = new DateTimeOffset(2026, 6, 24, hour, minute, 0, TimeSpan.Zero),
        Label = label,
    };

    [Fact]
    public void MenuLabel_WithoutLabel_IsTheReadableStartTime()
    {
        Assert.Equal("Wed 24 Jun 10:00", At(10, 0).MenuLabel());
    }

    [Fact]
    public void MenuLabel_WithLabel_AppendsItAfterTheTime()
    {
        Assert.Equal("Wed 24 Jun 14:30 · Standup", At(14, 30, "Standup").MenuLabel());
    }

    [Fact]
    public void MenuLabel_BlankLabel_IsTreatedAsNoLabel()
    {
        Assert.Equal("Wed 24 Jun 09:05", At(9, 5, "   ").MenuLabel());
    }
}
