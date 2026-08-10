using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Round-trips the Past-meetings history (#168) through real files in an injected
/// directory — the sibling of <see cref="MeetingStateStoreTests"/>, portable for the same
/// reason (session ids + timestamps, no secret). The model and its append/dedup/truncate
/// rules are covered by <see cref="MeetingHistoryTests"/>; this pins the file IO.
/// </summary>
public class MeetingHistoryStoreTests : IDisposable
{
    private readonly string _dir =
        Path.Join(Path.GetTempPath(), $"tapscribe-history-{Guid.NewGuid():N}");

    private static MeetingRecord Rec(string session) =>
        new() { SessionId = session, StartedAt = new DateTimeOffset(2026, 6, 24, 10, 0, 0, TimeSpan.Zero) };

    [Fact]
    public void SaveThenLoad_RoundTripsTheHistory_InTheInjectedDirectory()
    {
        var store = new MeetingHistoryStore(_dir);

        store.Save(MeetingHistory.Empty.Append(Rec("meet-A")));

        Assert.Equal(["meet-A"], store.Load().Meetings.Select(m => m.SessionId));
    }

    public void Dispose()
    {
        if (Directory.Exists(_dir))
            Directory.Delete(_dir, recursive: true);
    }
}
