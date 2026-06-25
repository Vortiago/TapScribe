using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// Round-trips the Past-meetings history (#168) through an actual file (a temp path),
/// the sibling of <see cref="MeetingStateStoreTests"/>. The model + append/dedup/truncate
/// + defensive parse are covered cross-platform by <c>MeetingHistoryTests</c> in the Core
/// suite; this pins the file IO (save / load / append, best-effort, corrupt→empty) on the
/// Windows job.
/// </summary>
public class MeetingHistoryStoreTests : IDisposable
{
    private readonly string _path =
        Path.Join(Path.GetTempPath(), $"tapscribe-history-{Guid.NewGuid():N}.json");

    private static MeetingRecord Rec(string session) =>
        new() { SessionId = session, StartedAt = new DateTimeOffset(2026, 6, 24, 10, 0, 0, TimeSpan.Zero) };

    [Fact]
    public void Load_MissingFile_ReturnsEmpty()
    {
        Assert.Empty(MeetingHistoryStore.Load(_path).Meetings); // fresh temp GUID, not yet written
    }

    [Fact]
    public void SaveThenLoad_RoundTripsTheHistory()
    {
        MeetingHistoryStore.Save(MeetingHistory.Empty.Append(Rec("meet-A")), _path);

        MeetingHistory loaded = MeetingHistoryStore.Load(_path);

        Assert.Equal(["meet-A"], loaded.Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Append_OnAMissingFile_CreatesItWithTheRecord()
    {
        MeetingHistoryStore.Append(Rec("meet-A"), _path);

        Assert.Equal(["meet-A"], MeetingHistoryStore.Load(_path).Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Append_AddsNewestFirst_AndPersists()
    {
        MeetingHistoryStore.Append(Rec("meet-A"), _path);
        MeetingHistoryStore.Append(Rec("meet-B"), _path);

        Assert.Equal(["meet-B", "meet-A"], MeetingHistoryStore.Load(_path).Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Load_ACorruptFile_ReturnsEmpty()
    {
        File.WriteAllText(_path, "{ this is not valid history json");

        Assert.Empty(MeetingHistoryStore.Load(_path).Meetings);
    }

    [Fact]
    public void Append_OverwritesACorruptFile_WithAFreshHistory()
    {
        File.WriteAllText(_path, "garbage");

        MeetingHistoryStore.Append(Rec("meet-A"), _path); // load degrades to empty, then appends

        Assert.Equal(["meet-A"], MeetingHistoryStore.Load(_path).Meetings.Select(m => m.SessionId));
    }

    public void Dispose()
    {
        if (File.Exists(_path))
            File.Delete(_path);
    }
}
