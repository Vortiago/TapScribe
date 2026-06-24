using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace TapScribe.Bridge.Core;

/// <summary>
/// HTTP client for the Recorder's tap-token control endpoints. Unlike the
/// WebSocket handshake, an HTTP request can set arbitrary headers, so the tap
/// token is carried as `Authorization: Bearer &lt;token&gt;` (auth.py:check_tap_bearer).
///
/// Present per the issue's build list. The #103 demo lands in the global current
/// session and does not call this; it is the seam later slices (per-bridge
/// detached sessions, end-of-meeting pipeline) build on.
/// </summary>
public sealed class ControlClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly bool _ownsHttp;
    private readonly Uri _baseUri;
    private readonly string _token;

    public ControlClient(
        string host, int port, bool tls, string token, HttpClient? http = null, bool allowSelfSignedCert = false)
    {
        // Same host tolerance as the /tap URI: a pasted scheme/port/path can't
        // produce a malformed base URI (see TapConnectionOptions.NormalizeHost).
        _baseUri = new UriBuilder
        {
            Scheme = tls ? "https" : "http",
            Host = TapConnectionOptions.NormalizeHost(host),
            Port = port,
        }.Uri;
        _token = token;
        _ownsHttp = http is null;
        // Opt-in insecure testing path (issue #147): accept any server cert ONLY when we
        // own the HttpClient and Tls && AllowSelfSignedCert. An injected HttpClient keeps
        // its caller's TLS policy — its handler is fixed at construction and not ours to
        // weaken. See InsecureTls.
        _http = http ?? (tls && allowSelfSignedCert ? InsecureTls.CreateInsecureHttpClient() : new HttpClient());
    }

    /// <summary>
    /// POST /api/tap/new-session with {"detached": true} and return the new
    /// detached session id. Pass that id as <see cref="TapConnectionOptions.Session"/>
    /// so the bridge's taps land in their own session.
    /// </summary>
    public async Task<string> CreateDetachedSessionAsync(CancellationToken cancellationToken = default)
    {
        using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(_baseUri, "/api/tap/new-session"))
        {
            Content = new StringContent("{\"detached\": true}", Encoding.UTF8, "application/json"),
        };
        if (!string.IsNullOrEmpty(_token))
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", _token);

        using HttpResponseMessage response = await _http.SendAsync(request, cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();

        await using Stream body = await response.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
        using JsonDocument doc = await JsonDocument.ParseAsync(body, cancellationToken: cancellationToken).ConfigureAwait(false);
        if (!doc.RootElement.TryGetProperty("session", out JsonElement session) || session.GetString() is not { } id)
            throw new InvalidOperationException("new-session response did not contain a 'session' id.");
        return id;
    }

    /// <summary>
    /// GET /health (auth-exempt) to check the Recorder is reachable. Throws
    /// <see cref="HttpRequestException"/> when the host can't be resolved/reached
    /// or returns a non-success status. No token is sent — this is a pure
    /// reachability probe.
    /// </summary>
    public async Task CheckHealthAsync(CancellationToken cancellationToken = default)
    {
        using HttpResponseMessage response =
            await _http.GetAsync(new Uri(_baseUri, "/health"), cancellationToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
    }

    public void Dispose()
    {
        if (_ownsHttp)
            _http.Dispose();
    }
}
