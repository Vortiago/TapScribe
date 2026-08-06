using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Hosting.Server.Features;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// A fuller in-process stand-in for the Python Recorder: it serves the real <c>/tap</c>
/// WebSocket AND the tap-token control endpoints (<c>/api/tap/new-session</c> + the
/// end-of-meeting pipeline) with a STATEFUL pipeline — a real one-job-per-session rule
/// (a concurrent trigger gets a genuine 409) and a stage progression
/// strip → transcribe → summarize → done served across successive polls.
///
/// Unlike the scripted <see cref="ControlClientTests"/>' servers (which return canned
/// bodies to unit-test parsing and the controller in isolation), this drives the WHOLE
/// flow over real sockets, so a test can mint a detached session, stream real taps into
/// it, End the meeting (real Drain), trigger the pipeline, and poll to the summary — the
/// integration of <see cref="CaptureOrchestrator"/> + <see cref="TapClient"/> +
/// <see cref="ControlClient"/> + <see cref="MeetingController"/> as one regression test.
/// </summary>
internal sealed class FakeRecorder : IAsyncDisposable
{
    /// <summary>The summary text the pipeline resolves to on the terminal poll — distinct
    /// per session, so a test can prove a meeting receives ITS OWN session's summary and
    /// not another's (the recorder persists one summary per session-summary.json).</summary>
    public static string SummaryFor(string session) => $"decided to ship — {session}";

    private sealed class TapRecord
    {
        public required string Session { get; init; }
        public required string Identity { get; init; }
        public int Frames { get; set; }
        public bool ClosedNormally { get; set; }
    }

    private readonly WebApplication _app;
    private readonly object _lock = new();
    private readonly List<TapRecord> _taps = [];
    private readonly Dictionary<string, int> _polls = new(StringComparer.Ordinal);
    private readonly Dictionary<string, int> _triggers = new(StringComparer.Ordinal);
    private readonly HashSet<string> _active = new(StringComparer.Ordinal); // sessions with a job in flight
    private readonly string _sessionId;
    private readonly bool _failTranscribe;
    private int _newSessionCount;

    public int Port { get; }

    private FakeRecorder(WebApplication app, int port, string sessionId, bool failTranscribe)
    {
        _app = app;
        Port = port;
        _sessionId = sessionId;
        _failTranscribe = failTranscribe;
    }

    public int NewSessionCount
    {
        get { lock (_lock) return _newSessionCount; }
    }

    public int TriggerCount(string session)
    {
        lock (_lock)
            return _triggers.TryGetValue(session, out int n) ? n : 0;
    }

    public int FramesFor(string session, string identity)
    {
        lock (_lock)
            return _taps.Where(t => t.Session == session && t.Identity == identity).Sum(t => t.Frames);
    }

    /// <summary>True once at least one tap opened for the session and ALL of them closed
    /// cleanly — the observable proof that the orchestrator's Drain ran.</summary>
    public bool AllTapsClosed(string session)
    {
        lock (_lock)
        {
            List<TapRecord> forSession = _taps.Where(t => t.Session == session).ToList();
            return forSession.Count > 0 && forSession.All(t => t.ClosedNormally);
        }
    }

    public static async Task<FakeRecorder> StartAsync(
        string sessionId = "2026-06-24T10-00-00", bool failTranscribe = false)
    {
        WebApplicationBuilder builder = WebApplication.CreateBuilder();
        builder.WebHost.UseUrls("http://127.0.0.1:0");
        WebApplication app = builder.Build();
        app.UseWebSockets();

        FakeRecorder? self = null;

        app.Map("/tap", async (HttpContext ctx) =>
        {
            if (!ctx.WebSockets.IsWebSocketRequest)
            {
                ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
                return;
            }

            string? chosen = ctx.WebSockets.WebSocketRequestedProtocols
                .FirstOrDefault(p => p.StartsWith(TapWire.SubprotocolPrefix, StringComparison.Ordinal));
            using WebSocket ws = chosen is null
                ? await ctx.WebSockets.AcceptWebSocketAsync()
                : await ctx.WebSockets.AcceptWebSocketAsync(chosen);

            var tap = new TapRecord
            {
                Session = ctx.Request.Query["session"].ToString(),
                Identity = ctx.Request.Query["identity"].ToString(),
            };
            lock (self!._lock)
                self._taps.Add(tap);

            var buffer = new byte[4096];
            try
            {
                while (ws.State == WebSocketState.Open)
                {
                    WebSocketReceiveResult result = await ws.ReceiveAsync(buffer, CancellationToken.None);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "ok", CancellationToken.None);
                        lock (self._lock)
                            tap.ClosedNormally = true;
                        break;
                    }
                    if (result.MessageType == WebSocketMessageType.Binary && result.Count >= 4)
                        lock (self._lock)
                            tap.Frames++;
                }
            }
            catch (WebSocketException)
            {
                // Client reset under us (e.g. a hard teardown): the frames recorded so far stand.
            }
        });

        app.MapPost("/api/tap/new-session", async (HttpContext ctx) =>
        {
            lock (self!._lock)
                self._newSessionCount++;
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync($"{{\"ok\":true,\"detached\":true,\"session\":\"{self._sessionId}\"}}");
        });

        app.MapPost("/api/tap/sessions/{session}/pipeline", async (string session, HttpContext ctx) =>
        {
            bool busy;
            lock (self!._lock)
            {
                self._triggers[session] = (self._triggers.TryGetValue(session, out int n) ? n : 0) + 1;
                busy = !self._active.Add(session); // one job per session: a second trigger conflicts
            }
            ctx.Response.ContentType = "application/json";
            if (busy)
            {
                ctx.Response.StatusCode = StatusCodes.Status409Conflict;
                await ctx.Response.WriteAsync($"{{\"detail\":\"session '{session}' already has a job in flight\"}}");
                return;
            }
            ctx.Response.StatusCode = StatusCodes.Status202Accepted;
            await ctx.Response.WriteAsync($"{{\"ok\":true,\"session\":\"{session}\",\"state\":\"running\"}}");
        });

        app.MapGet("/api/tap/sessions/{session}/pipeline", async (string session, HttpContext ctx) =>
        {
            int poll;
            bool fail;
            lock (self!._lock)
            {
                poll = (self._polls.TryGetValue(session, out int n) ? n : 0) + 1;
                self._polls[session] = poll;
                fail = self._failTranscribe;
            }
            ctx.Response.ContentType = "application/json";
            await ctx.Response.WriteAsync(PollBody(session, poll, fail));
        });

        await app.StartAsync();
        string address = app.Services.GetRequiredService<IServer>()
            .Features.Get<IServerAddressesFeature>()!.Addresses.First();
        self = new FakeRecorder(app, new Uri(address).Port, sessionId, failTranscribe);
        return self;
    }

    // A realistic stage progression keyed on the poll count: strip → transcribe (per WAV)
    // → summarize → done. A failTranscribe run fails at the transcribe stage instead.
    private static string PollBody(string session, int poll, bool fail)
    {
        if (fail && poll >= 2)
            return $"{{\"ok\":true,\"session\":\"{session}\",\"state\":\"failed\",\"stage\":\"transcribe\"," +
                   "\"error\":\"no speech\",\"error_kind\":\"NoUsableWavs\"}";
        return poll switch
        {
            1 => Running(session, "strip", "stripping"),
            2 => Running(session, "transcribe", "transcribing", 1, 2, "a.wav"),
            3 => Running(session, "transcribe", "transcribing", 2, 2, "b.wav"),
            4 => Running(session, "summarize", "summarizing"),
            _ => $"{{\"ok\":true,\"session\":\"{session}\",\"state\":\"done\"," +
                 $"\"summary\":{{\"summary\":\"{SummaryFor(session)}\",\"source\":\"local\"}}}}",
        };
    }

    private static string Running(string session, string stage, string status, int current = 0, int total = 0, string? file = null) =>
        $"{{\"ok\":true,\"session\":\"{session}\",\"state\":\"running\",\"stage\":\"{stage}\",\"status\":\"{status}\"," +
        $"\"current\":{current},\"total\":{total},\"current_file\":{(file is null ? "null" : $"\"{file}\"")}}}";

    public async ValueTask DisposeAsync()
    {
        await _app.StopAsync();
        await _app.DisposeAsync();
    }
}
