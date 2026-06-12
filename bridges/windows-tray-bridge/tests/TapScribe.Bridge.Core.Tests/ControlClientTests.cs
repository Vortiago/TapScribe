using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Exercises the tap-token control client against an in-process Kestrel
/// /api/tap/new-session endpoint (same approach as the /tap WebSocket test), so
/// the bearer-auth + detached-session request/response contract is covered
/// without the Python Recorder.
/// </summary>
public class ControlClientTests
{
    [Fact]
    public async Task CreateDetachedSession_ReturnsSessionIdFromResponse()
    {
        await using FakeControlServer server = await FakeControlServer.StartAsync(
            statusCode: 200, responseJson: "{\"ok\": true, \"detached\": true, \"session\": \"2026-06-12T10-00-00\"}");
        using var http = new HttpClient();
        using var client = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);

        string session = await client.CreateDetachedSessionAsync();

        Assert.Equal("2026-06-12T10-00-00", session);
    }

    [Fact]
    public async Task CreateDetachedSession_PostsDetachedTrue_WithBearerToken()
    {
        await using FakeControlServer server = await FakeControlServer.StartAsync(
            statusCode: 200, responseJson: "{\"session\": \"s1\"}");
        using var http = new HttpClient();
        using var client = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);

        await client.CreateDetachedSessionAsync();

        Assert.Equal("POST", server.CapturedMethod);
        Assert.Equal("/api/tap/new-session", server.CapturedPath);
        Assert.Equal("Bearer tok-abc", server.CapturedAuthorization);
        Assert.Contains("\"detached\"", server.CapturedBody);
        Assert.Contains("true", server.CapturedBody);
    }

    [Fact]
    public async Task CreateDetachedSession_NoAuthMode_SendsNoAuthorizationHeader()
    {
        await using FakeControlServer server = await FakeControlServer.StartAsync(
            statusCode: 200, responseJson: "{\"session\": \"s1\"}");
        using var http = new HttpClient();
        using var client = new ControlClient("127.0.0.1", server.Port, tls: false, token: "", http);

        await client.CreateDetachedSessionAsync();

        Assert.True(string.IsNullOrEmpty(server.CapturedAuthorization));
    }

    [Fact]
    public async Task CreateDetachedSession_ThrowsWhenResponseHasNoSessionId()
    {
        await using FakeControlServer server = await FakeControlServer.StartAsync(
            statusCode: 200, responseJson: "{\"ok\": true}"); // no "session"
        using var http = new HttpClient();
        using var client = new ControlClient("127.0.0.1", server.Port, tls: false, token: "tok-abc", http);

        await Assert.ThrowsAsync<InvalidOperationException>(() => client.CreateDetachedSessionAsync());
    }

    /// <summary>Minimal in-process Recorder-like control endpoint for one request.</summary>
    private sealed class FakeControlServer : IAsyncDisposable
    {
        private readonly WebApplication _app;

        public int Port { get; }
        public string CapturedMethod { get; private set; } = "";
        public string CapturedPath { get; private set; } = "";
        public string CapturedAuthorization { get; private set; } = "";
        public string CapturedBody { get; private set; } = "";

        private FakeControlServer(WebApplication app, int port)
        {
            _app = app;
            Port = port;
        }

        public static async Task<FakeControlServer> StartAsync(int statusCode, string responseJson)
        {
            WebApplicationBuilder builder = WebApplication.CreateBuilder();
            builder.WebHost.UseUrls("http://127.0.0.1:0");
            WebApplication app = builder.Build();

            FakeControlServer? self = null;
            app.MapPost("/api/tap/new-session", async (HttpContext context) =>
            {
                self!.CapturedMethod = context.Request.Method;
                self.CapturedPath = context.Request.Path;
                self.CapturedAuthorization = context.Request.Headers.Authorization.ToString();
                using var reader = new StreamReader(context.Request.Body);
                self.CapturedBody = await reader.ReadToEndAsync();

                context.Response.StatusCode = statusCode;
                context.Response.ContentType = "application/json";
                await context.Response.WriteAsync(responseJson);
            });

            await app.StartAsync();
            string address = app.Services.GetRequiredService<IServer>()
                .Features.Get<IServerAddressesFeature>()!.Addresses.First();
            self = new FakeControlServer(app, new Uri(address).Port);
            return self;
        }

        public async ValueTask DisposeAsync()
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }
    }
}
