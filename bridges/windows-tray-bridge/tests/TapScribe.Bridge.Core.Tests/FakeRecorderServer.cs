using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// In-process Kestrel stand-in for the Recorder's tap-token pipeline endpoints
/// (<c>POST</c>/<c>GET /api/tap/sessions/{session}/pipeline</c>, plus
/// <c>/api/tap/new-session</c>), so the end-of-meeting flow is driven over real
/// HTTP/loopback with no Python Recorder — the highest-fidelity test reachable
/// without a Windows desktop and a real microphone.
///
/// <para><c>GET</c> serves a SCRIPTED sequence of poll bodies (the last entry
/// repeats once the script is exhausted), so a single run can walk running → …
/// → done. <c>POST</c> returns a configurable status (202 accept / 409 busy),
/// fires <see cref="OnTrigger"/> the instant it arrives, and captures the bearer
/// header + body so the auth and drain-before-trigger contract is asserted.</para>
/// </summary>
internal sealed class FakeRecorderServer : IAsyncDisposable
{
    private readonly WebApplication _app;
    private readonly object _lock = new();
    private readonly Queue<(int Status, string Body)> _pollScript;
    private readonly int _triggerStatus;
    private (int Status, string Body) _lastPoll = (200, "{\"ok\":true,\"state\":\"idle\"}");

    public int Port { get; }
    public int NewSessionCount { get; private set; }
    public int TriggerCount { get; private set; }
    public int PollCount { get; private set; }
    public string TriggerAuthorization { get; private set; } = "";
    public string TriggerPath { get; private set; } = "";
    public string TriggerBody { get; private set; } = "";
    public string PollAuthorization { get; private set; } = "";

    /// <summary>Invoked (outside the lock) the instant a pipeline POST is received —
    /// lets a test record drain-vs-trigger ordering against its own event log.</summary>
    public Action? OnTrigger { get; set; }

    private FakeRecorderServer(WebApplication app, int port, int triggerStatus, Queue<(int, string)> pollScript)
    {
        _app = app;
        Port = port;
        _triggerStatus = triggerStatus;
        _pollScript = pollScript;
    }

    public static async Task<FakeRecorderServer> StartAsync(
        string sessionId = "2026-06-24T10-00-00",
        int triggerStatus = 202,
        IEnumerable<(int Status, string Body)>? pollScript = null)
    {
        WebApplicationBuilder builder = WebApplication.CreateBuilder();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        WebApplication app = builder.Build();

        var queue = new Queue<(int, string)>(pollScript ?? []);
        FakeRecorderServer? self = null;

        app.MapPost("/api/tap/new-session", async (HttpContext ctx) =>
        {
            lock (self!._lock)
                self.NewSessionCount++;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync($"{{\"ok\":true,\"detached\":true,\"session\":\"{sessionId}\"}}");
        });

        app.MapPost("/api/tap/sessions/{session}/pipeline", async (HttpContext ctx) =>
        {
            using var reader = new StreamReader(ctx.Request.Body);
            string body = await reader.ReadToEndAsync();
            int status;
            lock (self!._lock)
            {
                self.TriggerCount++;
                self.TriggerAuthorization = ctx.Request.Headers.Authorization.ToString();
                self.TriggerPath = ctx.Request.Path;
                self.TriggerBody = body;
                status = self._triggerStatus;
            }
            self.OnTrigger?.Invoke();
            ctx.Response.StatusCode = status;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync($"{{\"ok\":true,\"session\":\"{sessionId}\",\"state\":\"running\"}}");
        });

        app.MapGet("/api/tap/sessions/{session}/pipeline", async (HttpContext ctx) =>
        {
            (int Status, string Body) next;
            lock (self!._lock)
            {
                self.PollCount++;
                self.PollAuthorization = ctx.Request.Headers.Authorization.ToString();
                if (self._pollScript.Count > 0)
                    self._lastPoll = self._pollScript.Dequeue();
                next = self._lastPoll;
            }
            ctx.Response.StatusCode = next.Status;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync(next.Body);
        });

        await app.StartAsync();
        string address = app.Services.GetRequiredService<IServer>()
            .Features.Get<IServerAddressesFeature>()!.Addresses.First();
        self = new FakeRecorderServer(app, new Uri(address).Port, triggerStatus, queue);
        return self;
    }

    public async ValueTask DisposeAsync()
    {
        await _app.StopAsync();
        await _app.DisposeAsync();
    }
}
