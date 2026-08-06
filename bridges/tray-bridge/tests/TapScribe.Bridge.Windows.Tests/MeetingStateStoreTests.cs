using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// Round-trips the restart-resume state through an actual file (a temp path), the
/// sibling of <see cref="BridgeSettingsStoreTests"/>. The serialization itself is
/// covered cross-platform by <c>MeetingStateTests</c> in the Core suite; this pins
/// the file IO (save / load / clear) on the Windows job.
/// </summary>
public class MeetingStateStoreTests : IDisposable
{
    private readonly string _path =
        Path.Join(Path.GetTempPath(), $"tapscribe-meeting-{Guid.NewGuid():N}.json");

    [Fact]
    public void SaveThenLoad_RoundTripsTheSessionId()
    {
        MeetingStateStore.Save(new MeetingState { SessionId = "2026-06-24T10-00-00" }, _path);

        MeetingState? loaded = MeetingStateStore.Load(_path);

        Assert.Equal("2026-06-24T10-00-00", loaded?.SessionId);
    }

    [Fact]
    public void Load_MissingFile_ReturnsNull()
    {
        Assert.Null(MeetingStateStore.Load(_path)); // fresh temp GUID, not yet written
    }

    [Fact]
    public void Clear_RemovesTheState_SoLoadReturnsNull()
    {
        MeetingStateStore.Save(new MeetingState { SessionId = "s1" }, _path);

        MeetingStateStore.Clear(_path);

        Assert.Null(MeetingStateStore.Load(_path));
    }

    [Fact]
    public void Clear_OnAMissingFile_DoesNotThrow()
    {
        MeetingStateStore.Clear(_path); // no file yet — must be a no-op
    }

    public void Dispose()
    {
        if (File.Exists(_path))
            File.Delete(_path);
    }
}
