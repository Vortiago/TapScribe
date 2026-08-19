using System.Net.WebSockets;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

public class TapConnectionOptionsTests
{
    [Fact]
    public void DefaultIdentity_StaysTheOnDiskContract()
    {
        // The identity is the WAV filename slug and the key the Recorder
        // attributes recordings under. It deliberately still says
        // "windows-tray" after the bridges/tray-bridge/ directory rename —
        // changing it re-attributes the tray as a brand-new speaker (fresh
        // people-registry entry, old recordings orphaned), so a change here
        // needs a migration, not a rename.
        Assert.Equal("windows-tray", new TapConnectionOptions().Identity);
    }

    [Fact]
    public void BuildTapUri_NoTls_HasWsSchemeAndTapPathAndEncodedParams()
    {
        var options = new TapConnectionOptions { Host = "localhost", Port = 8001, Identity = "alice", Name = "Alice B" };

        Uri uri = options.BuildTapUri();

        Assert.Equal("ws", uri.Scheme);
        Assert.Equal("localhost", uri.Host);
        Assert.Equal(8001, uri.Port);
        Assert.Equal("/tap", uri.AbsolutePath);
        Assert.Contains("identity=alice", uri.Query);
        Assert.Contains("name=Alice%20B", uri.Query); // space is percent-encoded
    }

    [Fact]
    public void BuildTapUri_Tls_UsesWss()
    {
        var options = new TapConnectionOptions { Tls = true };

        Assert.Equal("wss", options.BuildTapUri().Scheme);
    }

    [Fact]
    public void BuildTapUri_IncludesUtteranceAndSession_OnlyWhenSet()
    {
        var with = new TapConnectionOptions { UtteranceId = "abc123", Session = "2026-06-12T10-00-00" };
        var without = new TapConnectionOptions();

        string withQuery = with.BuildTapUri().Query;
        string withoutQuery = without.BuildTapUri().Query;

        Assert.Contains("utterance_id=abc123", withQuery);
        Assert.Contains("session=2026-06-12T10-00-00", withQuery);
        Assert.DoesNotContain("utterance_id=", withoutQuery);
        Assert.DoesNotContain("session=", withoutQuery);
    }

    [Fact]
    public void BuildSubprotocol_ReturnsNull_WhenTokenEmpty()
    {
        Assert.Null(new TapConnectionOptions { Token = "" }.BuildSubprotocol());
    }

    [Fact]
    public void BuildSubprotocol_PrefixesToken()
    {
        var options = new TapConnectionOptions { Token = "AaLDg9xmHNNoi-Ug" };

        Assert.Equal("tapscribe.v1.tap.AaLDg9xmHNNoi-Ug", options.BuildSubprotocol());
    }

    [Theory]
    [InlineData("AaLDg9xmHNNoi-Ug")] // a real secrets.token_urlsafe(12) shape (base64url: '-')
    [InlineData("abc_DEF-123.xyz")]  // exercises every base64url + dot char
    public void Subprotocol_IsAcceptedByClientWebSocket(string token)
    {
        // ClientWebSocket validates subprotocols against the RFC token charset and
        // throws on invalid chars. The Recorder mints tokens with token_urlsafe
        // (base64url), so the joined subprotocol must pass without throwing.
        string subprotocol = new TapConnectionOptions { Token = token }.BuildSubprotocol()!;
        using var ws = new ClientWebSocket();

        Exception? ex = Record.Exception(() => ws.Options.AddSubProtocol(subprotocol));

        Assert.Null(ex);
    }

    [Fact]
    public void BuildTapUri_PlainDnsHost_ProducesCleanWsUri()
    {
        var options = new TapConnectionOptions { Host = "recorder.example.com", Port = 8001, Identity = "x" };

        Uri uri = options.BuildTapUri();

        Assert.Equal("ws", uri.Scheme);
        Assert.Equal("recorder.example.com", uri.Host);
        Assert.Equal(8001, uri.Port);
        Assert.Equal("/tap", uri.AbsolutePath);
    }

    [Theory]
    [InlineData("ws://recorder.example.com")]
    [InlineData("wss://recorder.example.com")]
    [InlineData("http://recorder.example.com")]
    [InlineData("recorder.example.com:9000")]
    [InlineData("recorder.example.com/")]
    [InlineData("  recorder.example.com  ")]
    public void BuildTapUri_HostWithSchemeOrPortOrPath_NormalizesToBareHost(string raw)
    {
        // Users paste a DNS name with a scheme/port/path (or whitespace); the bare
        // host must be extracted and the Port/TLS fields stay authoritative.
        var options = new TapConnectionOptions { Host = raw, Port = 8001, Identity = "x" };

        Uri uri = options.BuildTapUri();

        Assert.Equal("recorder.example.com", uri.Host);
        Assert.Equal(8001, uri.Port);
    }

    [Fact]
    public void BuildTapUri_PercentEncodesIdentity_SoReservedCharsCannotInjectParams()
    {
        // An identity containing '&' or '=' must be percent-encoded, not break out
        // into extra query parameters.
        var options = new TapConnectionOptions { Identity = "a&b=c" };

        string query = options.BuildTapUri().Query;

        Assert.Contains("identity=a%26b%3Dc", query);
        Assert.DoesNotContain("identity=a&b=c", query);
    }

    [Fact]
    public void BuildTapUri_AlwaysDeclaresTapMode_DefaultingToSingle()
    {
        Assert.Contains("tap_mode=single", new TapConnectionOptions().BuildTapUri().Query);
    }

    [Fact]
    public void BuildTapUri_CarriesAMultiPersonDeclaration()
    {
        var options = new TapConnectionOptions { Mode = TapConnectionOptions.TapModeMulti };

        Assert.Contains("tap_mode=multi", options.BuildTapUri().Query);
    }

    [Fact]
    public void TapModeForFlow_TreatsLoopbackAsMultiPersonAndTheMicAsSingle()
    {
        // A Render device is the far end of the meeting; Capture is the operator.
        Assert.Equal(TapConnectionOptions.TapModeMulti, TapConnectionOptions.TapModeForFlow(DeviceFlow.Render));
        Assert.Equal(TapConnectionOptions.TapModeSingle, TapConnectionOptions.TapModeForFlow(DeviceFlow.Capture));
    }
}
