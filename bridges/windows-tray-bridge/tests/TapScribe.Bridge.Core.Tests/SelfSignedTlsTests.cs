using System.Net;
using System.Net.Http;
using System.Net.WebSockets;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Proves the opt-in "Allow invalid / self-signed certificate" mode (issue #147) end to
/// end, in BOTH directions and for BOTH connection mechanisms, against an in-process
/// Kestrel Recorder serving a freshly-generated self-signed cert over real TLS:
///
/// <list type="bullet">
/// <item>HttpClient path (<see cref="ControlClient"/> GET /health) — accepts with the
///   flag, rejects without it.</item>
/// <item>WebSocket path (<see cref="TapClient"/> wss:// /tap) — accepts with the flag,
///   rejects without it.</item>
/// <item>The full <see cref="ConnectionTester"/> probe (both halves at once).</item>
/// <item>The "owned-HttpClient path only" scope: an INJECTED HttpClient keeps its own TLS
///   policy and is NOT weakened by the flag.</item>
/// </list>
///
/// Kestrel's HTTPS works identically on the ubuntu CI job, so this runs cross-platform.
/// </summary>
public class SelfSignedTlsTests
{
    // --- ControlClient over HTTPS (the HttpClient / DangerousAcceptAnyServerCertificateValidator path) ---

    [Fact]
    public async Task Health_WithAllowSelfSigned_SucceedsAgainstSelfSignedTls()
    {
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        // Owned HttpClient (http: null) + the opt-in: the insecure handler is built, so the
        // self-signed cert is accepted.
        using var client = new ControlClient(
            "127.0.0.1", server.Port, tls: true, token: "", http: null, allowSelfSignedCert: true);

        // No throw == the self-signed chain was accepted.
        await client.CheckHealthAsync();
    }

    [Fact]
    public async Task Health_WithoutAllowSelfSigned_FailsAgainstSelfSignedTls()
    {
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        using var client = new ControlClient(
            "127.0.0.1", server.Port, tls: true, token: "", http: null, allowSelfSignedCert: false);

        // Default validation rejects the untrusted self-signed chain.
        await Assert.ThrowsAsync<HttpRequestException>(() => client.CheckHealthAsync());
    }

    [Fact]
    public async Task Health_InjectedHttpClient_IsNotWeakenedByTheFlag()
    {
        // The "owned-HttpClient path only" guarantee: passing the flag must NOT reach into
        // a caller-supplied HttpClient (its handler is fixed and not ours to weaken). A
        // plain injected client keeps default validation and so still rejects.
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        using var injected = new HttpClient();
        using var client = new ControlClient(
            "127.0.0.1", server.Port, tls: true, token: "", http: injected, allowSelfSignedCert: true);

        await Assert.ThrowsAsync<HttpRequestException>(() => client.CheckHealthAsync());
    }

    // --- TapClient over WSS (the ClientWebSocket.RemoteCertificateValidationCallback path) ---

    [Fact]
    public async Task WssConnect_WithAllowSelfSigned_Succeeds()
    {
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        var options = new TapConnectionOptions
        {
            Host = "127.0.0.1", Port = server.Port, Tls = true, AllowSelfSignedCert = true, Identity = "alice",
        };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));

        await using var client = new TapClient(options);
        await client.ConnectAsync(cts.Token);

        Assert.Equal(WebSocketState.Open, client.State);
    }

    [Fact]
    public async Task WssConnect_WithoutAllowSelfSigned_Throws()
    {
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        var options = new TapConnectionOptions
        {
            Host = "127.0.0.1", Port = server.Port, Tls = true, AllowSelfSignedCert = false, Identity = "alice",
        };
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));

        await using var client = new TapClient(options);

        // ClientWebSocket surfaces the TLS handshake failure as a WebSocketException.
        await Assert.ThrowsAsync<WebSocketException>(() => client.ConnectAsync(cts.Token));
    }

    // --- The full Test-connection probe (both halves) ---

    [Fact]
    public async Task ConnectionTester_WithAllowSelfSigned_IsOk()
    {
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        var options = new TapConnectionOptions
        {
            Host = "127.0.0.1", Port = server.Port, Tls = true, AllowSelfSignedCert = true, Token = "tok",
        };

        ConnectionTestResult result = await ConnectionTester.TestAsync(options, http: null, cts.Token);

        Assert.True(result.Reachable);
        Assert.True(result.TokenAccepted);
        Assert.True(result.Ok);
    }

    [Fact]
    public async Task ConnectionTester_WithoutAllowSelfSigned_ReportsUnreachable()
    {
        await using TlsFakeRecorder server = await TlsFakeRecorder.StartAsync();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        var options = new TapConnectionOptions
        {
            Host = "127.0.0.1", Port = server.Port, Tls = true, AllowSelfSignedCert = false, Token = "tok",
        };

        ConnectionTestResult result = await ConnectionTester.TestAsync(options, http: null, cts.Token);

        // The /health half fails the TLS handshake, so the probe stops at "unreachable"
        // and never even checks the token.
        Assert.False(result.Reachable);
        Assert.False(result.TokenChecked);
        Assert.False(result.Ok);
        Assert.NotNull(result.ReachError);
    }

    /// <summary>
    /// In-process Recorder stub like <c>ConnectionTesterTests.FakeRecorder</c>, but bound
    /// over real TLS with a self-signed cert: GET /health (200) and a /tap WS that accepts.
    /// </summary>
    private sealed class TlsFakeRecorder : IAsyncDisposable
    {
        private readonly WebApplication _app;

        public int Port { get; }

        private TlsFakeRecorder(WebApplication app, int port)
        {
            _app = app;
            Port = port;
        }

        public static async Task<TlsFakeRecorder> StartAsync()
        {
            X509Certificate2 cert = CreateSelfSignedCert();
            WebApplicationBuilder builder = WebApplication.CreateBuilder();
            builder.WebHost.ConfigureKestrel(kestrel =>
                kestrel.Listen(IPAddress.Loopback, 0, listen => listen.UseHttps(cert)));
            WebApplication app = builder.Build();
            app.UseWebSockets();

            app.MapGet("/health", (HttpContext context) =>
            {
                context.Response.StatusCode = StatusCodes.Status200OK;
                return context.Response.WriteAsync("{\"status\":\"ok\"}");
            });

            app.Map("/tap", async (HttpContext context) =>
            {
                if (!context.WebSockets.IsWebSocketRequest)
                {
                    context.Response.StatusCode = StatusCodes.Status400BadRequest;
                    return;
                }

                string? chosen = context.WebSockets.WebSocketRequestedProtocols
                    .FirstOrDefault(p => p.StartsWith(TapWire.SubprotocolPrefix, StringComparison.Ordinal));
                using WebSocket ws = chosen is null
                    ? await context.WebSockets.AcceptWebSocketAsync()
                    : await context.WebSockets.AcceptWebSocketAsync(chosen);

                var buffer = new byte[1024];
                try
                {
                    while (ws.State == WebSocketState.Open)
                    {
                        WebSocketReceiveResult result = await ws.ReceiveAsync(buffer, CancellationToken.None);
                        if (result.MessageType == WebSocketMessageType.Close)
                        {
                            await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "ok", CancellationToken.None);
                            break;
                        }
                    }
                }
                catch (WebSocketException)
                {
                    // The probe aborts the socket after a successful handshake; expected.
                }
            });

            await app.StartAsync();
            string address = app.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!.Addresses.First();
            return new TlsFakeRecorder(app, new Uri(address).Port);
        }

        // A throwaway self-signed cert valid for loopback, generated per server so the test
        // is hermetic (no cert files, no machine store). Re-imported via PKCS#12 so the
        // private key is usable by Kestrel's TLS stack on every OS.
        private static X509Certificate2 CreateSelfSignedCert()
        {
            using var rsa = RSA.Create(2048);
            var request = new CertificateRequest(
                "CN=localhost", rsa, HashAlgorithmName.SHA256, RSASignaturePadding.Pkcs1);
            var san = new SubjectAlternativeNameBuilder();
            san.AddDnsName("localhost");
            san.AddIpAddress(IPAddress.Loopback);
            request.CertificateExtensions.Add(san.Build());

            using X509Certificate2 ephemeral = request.CreateSelfSigned(
                DateTimeOffset.UtcNow.AddDays(-1), DateTimeOffset.UtcNow.AddDays(365));
            return X509CertificateLoader.LoadPkcs12(ephemeral.Export(X509ContentType.Pkcs12), password: null);
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }
}
