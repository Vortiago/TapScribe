using System.Net.Http;
using System.Net.Sockets;
using System.Net.WebSockets;

namespace TapScribe.Bridge.Core;

/// <summary>The outcome of a "Test connection" probe.</summary>
public sealed record ConnectionTestResult(
    bool Reachable,
    string? ReachError,
    bool TokenChecked,
    bool TokenAccepted,
    string? TokenError)
{
    /// <summary>True when the Recorder is reachable and (if checked) the token was accepted.</summary>
    public bool Ok => Reachable && (!TokenChecked || TokenAccepted);

    /// <summary>A one-line, user-facing summary suitable for the settings dialog.</summary>
    public string Describe()
    {
        if (!Reachable)
            return $"Recorder unreachable: {ReachError}";
        if (!TokenChecked)
            return "Recorder reachable.";
        return TokenAccepted
            ? "Recorder reachable; tap token accepted."
            : $"Recorder reachable, but the tap token was rejected: {TokenError}";
    }
}

/// <summary>What a Settings dialog puts on its status line after a "Test connection".</summary>
/// <param name="Text">Shown verbatim.</param>
/// <param name="Ok">Whether to render it as success.</param>
public readonly record struct ConnectionTestOutcome(string Text, bool Ok);

/// <summary>
/// Probes a Recorder the way the SpatialChat bridge's popup does: a GET /health
/// reachability check (catches a bad host / DNS / port / TLS), then — only if
/// reachable — a /tap WebSocket handshake to confirm the tap token is accepted.
/// The handshake opens a probe tap and immediately closes it with zero bytes, so
/// the Recorder discards the empty recording (no session side effect).
/// </summary>
public static class ConnectionTester
{
    /// <summary>Run the probe under the shared timeout and describe every outcome, a throw
    /// included. Both dialogs run this fire-and-forget from a click, where an escaping exception
    /// is swallowed by the scheduler and strands the status line on "Testing...".</summary>
    /// <summary>
    /// Whether SOMETHING is serving a Recorder's port — a synchronous <c>GET /health</c> with
    /// a short deadline, answering false for every way of not being there.
    ///
    /// The discriminator a Bundle's host role needs when the Recorder it just spawned exits
    /// immediately: "someone else's Recorder holds this port" versus "this install is
    /// broken" (ADR-0022). Here rather than in each tray shell, where it had been written
    /// twice character-for-character, and here rather than in Bundle.Core, which must not
    /// reference Bridge.Core — a Bundle is not a Bridge. Beside <see cref="TestAsync"/>
    /// because both wrap the same call for the same question, and the filter below is
    /// deliberately the one that method's callers already keep.
    ///
    /// Loopback and no token by design: a Bundle IS the Recorder on its own machine, and
    /// <c>/health</c> takes no credential.
    /// </summary>
    /// <param name="http">The caller's client, so no handler is churned per probe.</param>
    public static bool AnswersOnLoopback(int port, HttpClient http, TimeSpan timeout)
    {
        try
        {
            using var control = new ControlClient("127.0.0.1", port, tls: false, token: "", http);
            using var deadline = new CancellationTokenSource(timeout);
            control.CheckHealthAsync(deadline.Token).GetAwaiter().GetResult();
            return true;
        }
        catch (Exception error) when (
            error is HttpRequestException or TaskCanceledException or OperationCanceledException
                or SocketException or InvalidOperationException)
        {
            // Nothing listening, or it did not answer in time. Either way: not somebody
            // else's healthy Recorder.
            return false;
        }
    }

    public static async Task<ConnectionTestOutcome> DescribeAsync(
        TapConnectionOptions options, HttpClient? http = null)
    {
        try
        {
            using var timeout = new CancellationTokenSource(SettingsBounds.ConnectionTestTimeout);
            ConnectionTestResult result = await TestAsync(options, http, timeout.Token)
                .ConfigureAwait(false);
            return new ConnectionTestOutcome(result.Describe(), result.Ok);
        }
        catch (Exception ex) when (ex is not OutOfMemoryException)
        {
            // Deliberately the widest filter, like BridgeSettingsStore's token read. TestAsync
            // answers a bad host, a refused token and a timeout as RESULTS, so what is left is a
            // malformed entry throwing below it. What is lost is the stack.
            return new ConnectionTestOutcome($"Test failed: {ex.Message}", Ok: false);
        }
    }

    /// <summary>
    /// The probe over an operator's settings, shaped as
    /// <see cref="BridgeDependencies.CheckConnection"/>: what both shells pass as their
    /// production pre-flight for Connect (ADR-0025). Named here rather than written as a
    /// lambda at each wiring site, because the two would be character-for-character the same
    /// and would drift apart the first time one of them learned something.
    ///
    /// Connect has no mint to round-trip the Recorder, so without this an unreachable
    /// Recorder or a refused token stays silent until the first person speaks.
    /// </summary>
    public static Task<ConnectionTestResult> CheckSettingsAsync(
        BridgeSettings settings, CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(settings);
        return TestAsync(settings.ToConnectionOptions(), http: null, cancellationToken);
    }

    public static async Task<ConnectionTestResult> TestAsync(
        TapConnectionOptions options, HttpClient? http = null, CancellationToken cancellationToken = default)
    {
        // 1) Reachability — GET /health (no auth needed). The self-signed opt-in rides
        //    through both probe halves so the test mirrors how a meeting will connect.
        using (var control = new ControlClient(
            options.Host, options.Port, options.Tls, options.Token, http, options.AllowSelfSignedCert))
        {
            try
            {
                await control.CheckHealthAsync(cancellationToken).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is HttpRequestException or SocketException or InvalidOperationException or OperationCanceledException)
            {
                return new ConnectionTestResult(Reachable: false, ReachError: Describe(ex), TokenChecked: false, TokenAccepted: false, TokenError: null);
            }
        }

        // 2) Token — open a probe /tap (no session) and close it. A successful
        //    upgrade means the token was accepted; a 4401 refusal surfaces as a
        //    WebSocketException.
        var probe = new TapConnectionOptions
        {
            Host = options.Host,
            Port = options.Port,
            Tls = options.Tls,
            AllowSelfSignedCert = options.AllowSelfSignedCert,
            Token = options.Token,
            Identity = "__probe__",
            Name = "probe",
        };
        var tap = new TapClient(probe);
        try
        {
            await tap.ConnectAsync(cancellationToken).ConfigureAwait(false);

            // The Recorder denies a bad token *before* accepting (close 4401 →
            // ConnectAsync threw above). Guard the other shape too: a server that
            // accepts then immediately closes must not read as "accepted". A close
            // within the window => rejected; silence => accepted.
            bool closedByServer = await tap
                .WaitForServerCloseAsync(TimeSpan.FromMilliseconds(400), cancellationToken)
                .ConfigureAwait(false);
            return closedByServer
                ? new ConnectionTestResult(Reachable: true, ReachError: null, TokenChecked: true, TokenAccepted: false, TokenError: "the Recorder closed the tap immediately (token rejected)")
                : new ConnectionTestResult(Reachable: true, ReachError: null, TokenChecked: true, TokenAccepted: true, TokenError: null);
        }
        catch (Exception ex) when (ex is WebSocketException or OperationCanceledException)
        {
            return new ConnectionTestResult(Reachable: true, ReachError: null, TokenChecked: true, TokenAccepted: false, TokenError: Describe(ex));
        }
        finally
        {
            await tap.DisposeAsync().ConfigureAwait(false);
        }
    }

    private static string Describe(Exception ex) =>
        ex.InnerException is { } inner ? $"{ex.Message} ({inner.Message})" : ex.Message;
}
