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

    public void Dispose()
    {
        if (Directory.Exists(_dir))
            Directory.Delete(_dir, recursive: true);
    }
}
