using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The serialization half of restart-resume lives in Core (Linux-tested); the
/// %APPDATA% file IO is the Windows store's job. Round-trip + defensive parse so a
/// corrupt state file never crashes the tray at boot.
/// </summary>
public class MeetingStateTests
{
    [Fact]
    public void Json_RoundTripsTheSessionId()
    {
        var state = new MeetingState { SessionId = "2026-06-24T10-00-00" };

        MeetingState? back = MeetingState.FromJson(state.ToJson());

        Assert.Equal("2026-06-24T10-00-00", back?.SessionId);
    }

    [Fact]
    public void FromJson_ReturnsNull_OnGarbage()
    {
        Assert.Null(MeetingState.FromJson("not json"));
    }

    [Fact]
    public void FromJson_ReturnsNull_WhenSessionIdIsMissing()
    {
        Assert.Null(MeetingState.FromJson("{\"other\":1}"));
    }
}
