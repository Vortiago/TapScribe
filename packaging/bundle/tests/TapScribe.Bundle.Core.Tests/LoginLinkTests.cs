using System.Net;
using System.Net.Http;
using System.Text;

namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// What "Open dashboard" hands the browser (ADR-0023), over a stubbed Recorder — so the
/// tray's half of the login link is covered where the shell it used to live in is never even
/// built. The server's half is covered on the Python side, including in a real browser
/// (`test_login_link_signs_the_browser_in_without_an_auth_dialog`); what these pin is the
/// contract between them and, above all, that every way it can fail still opens the dashboard.
/// </summary>
public class LoginLinkTests
{
    [Fact]
    public void AMintedLinkBecomesTheUrlTheBrowserIsHanded()
    {
        var recorder = new StubRecorder(HttpStatusCode.OK, """{"path": "/login?k=abc123"}""");

        string url = Url(recorder);

        Assert.Equal("http://localhost:8001/login?k=abc123", url);
    }

    [Fact]
    public void ThePasswordTravelsAsBasicAuth()
    {
        // Minting is Basic-gated on purpose: only somebody who could already reach the
        // dashboard can make a link. The tray is that somebody, by reading `.auth-password`.
        var recorder = new StubRecorder(HttpStatusCode.OK, """{"path": "/login?k=abc123"}""");

        Url(recorder, password: "hunter2");

        Assert.Equal("Basic", recorder.LastAuthScheme);
        Assert.Equal(
            "admin:hunter2",
            Encoding.UTF8.GetString(Convert.FromBase64String(recorder.LastAuthParameter!)));
    }

    [Fact]
    public void ARefusedMintOpensThePlainDashboardAndSaysWhy()
    {
        // The wrong password, or a Recorder that has not read its own yet. The operator meets
        // the prompt they met before this feature existed — not an error, and not nothing.
        var recorder = new StubRecorder(HttpStatusCode.Unauthorized, "");
        var log = new List<string>();

        string url = Url(recorder, log: log.Add);

        Assert.Equal("http://localhost:8001/", url);
        Assert.Single(log);
    }

    [Fact]
    public void ARecorderThatIsNotUpYetOpensThePlainDashboard()
    {
        // The commonest case by far: the operator clicks the menu while the Recorder is still
        // booting. A throw here would reach the shell's guard and show "something went wrong".
        var recorder = new StubRecorder(new HttpRequestException("Connection refused"));
        var log = new List<string>();

        string url = Url(recorder, log: log.Add);

        Assert.Equal("http://localhost:8001/", url);
        Assert.Single(log);
    }

    [Fact]
    public void ARecorderWithNoLoginLinkRouteOpensThePlainDashboard()
    {
        // An older Recorder under a newer tray: the route 404s, or answers something without a
        // `path`. Both are the same non-event — the tray is not the half that gets upgraded.
        var missing = new StubRecorder(HttpStatusCode.OK, """{"detail": "Not Found"}""");

        Assert.Equal("http://localhost:8001/", Url(missing));

        var garbled = new StubRecorder(HttpStatusCode.OK, "<html>not json</html>");

        Assert.Equal("http://localhost:8001/", Url(garbled));
    }

    [Fact]
    public void AnEmptyPathIsNotPastedOntoTheUrl()
    {
        var recorder = new StubRecorder(HttpStatusCode.OK, """{"path": ""}""");

        Assert.Equal("http://localhost:8001/", Url(recorder));
    }

    private static string Url(StubRecorder recorder, string password = "pw", Action<string>? log = null)
    {
        using var http = new HttpClient(recorder);
        return LoginLink.SignedInUrl(http, BundleDefaults.DashboardUrl, password, log ?? (_ => { }));
    }

    /// <summary>A Recorder that answers one canned way, and remembers the credential it was
    /// shown. A handler rather than a socket: what is under test is the request built and the
    /// answer read, and a real listener would add a port and a race to every case.</summary>
    private sealed class StubRecorder(HttpStatusCode status, string body) : HttpMessageHandler
    {
        private readonly Exception? _throws;

        public StubRecorder(Exception throws) : this(HttpStatusCode.OK, "") => _throws = throws;

        public string? LastAuthScheme { get; private set; }

        public string? LastAuthParameter { get; private set; }

        protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            ArgumentNullException.ThrowIfNull(request);
            LastAuthScheme = request.Headers.Authorization?.Scheme;
            LastAuthParameter = request.Headers.Authorization?.Parameter;
            if (_throws is not null)
                throw _throws;
            return new HttpResponseMessage(status) { Content = new StringContent(body) };
        }

        protected override Task<HttpResponseMessage> SendAsync(
            HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(Send(request, cancellationToken));
    }
}
