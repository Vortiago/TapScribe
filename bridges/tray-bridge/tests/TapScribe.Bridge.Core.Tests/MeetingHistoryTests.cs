using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The Past-meetings (#168) history model: a bounded, most-recent-first list of
/// <see cref="MeetingRecord"/>, append/dedup/truncate and defensive JSON. Lives in
/// Core (Linux-tested); the %APPDATA% file IO is the Windows store's job, exactly
/// like <see cref="MeetingState"/> ↔ <c>MeetingStateStore</c>.
/// </summary>
public class MeetingHistoryTests
{
    private static MeetingRecord Rec(string session, int minute = 0, string? label = null) => new()
    {
        SessionId = session,
        StartedAt = new DateTimeOffset(2026, 6, 24, 10, minute, 0, TimeSpan.Zero),
        Label = label,
    };

    [Fact]
    public void Append_PutsTheRecordInTheList()
    {
        MeetingHistory history = MeetingHistory.Empty.Append(Rec("meet-A"));

        Assert.Equal(["meet-A"], history.Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Append_IsNewestFirst()
    {
        MeetingHistory history = MeetingHistory.Empty.Append(Rec("meet-A")).Append(Rec("meet-B"));

        Assert.Equal(["meet-B", "meet-A"], history.Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Append_SameSession_MovesItToTheFront_WithoutDuplicating()
    {
        MeetingHistory history = MeetingHistory.Empty
            .Append(Rec("meet-A"))
            .Append(Rec("meet-B"))
            .Append(Rec("meet-A", minute: 30, label: "re-ended"));

        Assert.Equal(["meet-A", "meet-B"], history.Meetings.Select(m => m.SessionId));
        Assert.Equal("re-ended", history.Meetings[0].Label); // the newer entry replaced the old
    }

    [Fact]
    public void Append_TruncatesToMaxEntries_DroppingTheOldest()
    {
        MeetingHistory history = MeetingHistory.Empty;
        for (int i = 0; i < MeetingHistory.MaxEntries + 5; i++)
            history = history.Append(Rec($"meet-{i:D2}"));

        Assert.Equal(MeetingHistory.MaxEntries, history.Meetings.Count);
        Assert.Equal("meet-24", history.Meetings[0].SessionId);  // newest
        Assert.Equal("meet-05", history.Meetings[^1].SessionId); // oldest kept (00..04 dropped)
    }

    [Fact]
    public void Json_RoundTripsMeetings_NewestFirst()
    {
        MeetingHistory history = MeetingHistory.Empty
            .Append(Rec("meet-A", label: "kickoff"))
            .Append(Rec("meet-B"));

        MeetingHistory back = MeetingHistory.FromJson(history.ToJson());

        Assert.Equal(["meet-B", "meet-A"], back.Meetings.Select(m => m.SessionId));
        Assert.Equal("kickoff", back.Meetings[^1].Label);
        Assert.Equal(history.Meetings[0].StartedAt, back.Meetings[0].StartedAt);
    }

    [Fact]
    public void FromJson_ReturnsEmpty_OnGarbage()
    {
        Assert.Empty(MeetingHistory.FromJson("not json").Meetings);
    }

    [Fact]
    public void FromJson_ReturnsEmpty_WhenARecordIsMissingItsSession()
    {
        // All-or-nothing parse (a required session id missing): the whole file degrades
        // to no past meetings, mirroring MeetingState's defensive parse.
        Assert.Empty(MeetingHistory.FromJson("{\"meetings\":[{\"startedAt\":\"2026-06-24T10:00:00+00:00\"}]}").Meetings);
    }
}
