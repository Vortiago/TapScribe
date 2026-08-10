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

    [Fact]
    public void HistoryFileName_StaysTheOnDiskContract()
    {
        // Renaming the file silently discards every operator's Past-meetings list on
        // upgrade; a change here needs a migration, not a rename.
        Assert.Equal("meeting-history.json", MeetingHistoryStore.HistoryFileName);
    }

    [Fact]
    public void Load_AMissingFile_ReturnsEmpty()
    {
        Assert.Empty(new MeetingHistoryStore(_dir).Load().Meetings); // nothing written yet
    }

    [Fact]
    public void Append_OnAMissingFile_CreatesItWithTheRecord()
    {
        var store = new MeetingHistoryStore(_dir);

        store.Append(Rec("meet-A"));

        Assert.Equal(["meet-A"], store.Load().Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Append_AddsNewestFirst_AndPersists()
    {
        var store = new MeetingHistoryStore(_dir);

        store.Append(Rec("meet-A"));
        store.Append(Rec("meet-B"));

        Assert.Equal(["meet-B", "meet-A"], store.Load().Meetings.Select(m => m.SessionId));
    }

    [Fact]
    public void Load_ACorruptFile_ReturnsEmpty()
    {
        var store = new MeetingHistoryStore(_dir);
        Directory.CreateDirectory(_dir);
        File.WriteAllText(store.FilePath, "{ this is not valid history json");

        Assert.Empty(store.Load().Meetings);
    }

    [Fact]
    public void Append_OverwritesACorruptFile_WithAFreshHistory()
    {
        var store = new MeetingHistoryStore(_dir);
        Directory.CreateDirectory(_dir);
        File.WriteAllText(store.FilePath, "garbage");

        store.Append(Rec("meet-A")); // load degrades to empty, then appends

        Assert.Equal(["meet-A"], store.Load().Meetings.Select(m => m.SessionId));
    }

    public void Dispose()
    {
        if (Directory.Exists(_dir))
            Directory.Delete(_dir, recursive: true);
    }
}
