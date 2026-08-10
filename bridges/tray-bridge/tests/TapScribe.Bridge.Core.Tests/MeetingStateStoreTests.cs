using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Round-trips the restart-resume state through real files in an injected directory — the
/// sibling of <see cref="BridgeSettingsStoreTests"/>, and portable for the same reason:
/// there is no secret here (just a session id), so the only thing a platform contributes
/// is WHERE the directory is. The serialization itself is covered by
/// <see cref="MeetingStateTests"/>; this pins the file IO (save / load / clear).
/// </summary>
public class MeetingStateStoreTests : IDisposable
{
    private readonly string _dir =
        Path.Join(Path.GetTempPath(), $"tapscribe-meeting-{Guid.NewGuid():N}");

    [Fact]
    public void SaveThenLoad_RoundTripsTheSessionId_InTheInjectedDirectory()
    {
        var store = new MeetingStateStore(_dir);

        store.Save(new MeetingState { SessionId = "2026-06-24T10-00-00" });

        Assert.Equal("2026-06-24T10-00-00", store.Load()?.SessionId);
    }

    [Fact]
    public void StateFileName_StaysTheOnDiskContract()
    {
        // Renaming the file strands any in-flight meeting's restart-resume state on
        // upgrade; a change here needs a migration, not a rename.
        Assert.Equal("meeting-state.json", MeetingStateStore.StateFileName);
    }

    [Fact]
    public void Load_AMissingFile_ReturnsNull()
    {
        Assert.Null(new MeetingStateStore(_dir).Load()); // fresh temp GUID, nothing written
    }

    [Fact]
    public void Clear_RemovesTheState_SoLoadReturnsNull()
    {
        var store = new MeetingStateStore(_dir);
        store.Save(new MeetingState { SessionId = "s1" });

        store.Clear();

        Assert.Null(store.Load());
    }

    [Fact]
    public void Clear_OnAMissingFile_DoesNotThrow()
    {
        new MeetingStateStore(_dir).Clear(); // no file yet — must be a no-op
    }

    public void Dispose()
    {
        if (Directory.Exists(_dir))
            Directory.Delete(_dir, recursive: true);
    }
}
