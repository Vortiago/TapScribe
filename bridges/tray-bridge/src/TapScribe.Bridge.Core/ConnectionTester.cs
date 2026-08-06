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

/// <summary>
/// Probes a Recorder the way the SpatialChat bridge's popup does: a GET /health
/// reachability check (catches a bad host / DNS / port / TLS), then — only if
/// reachable — a /tap WebSocket handshake to confirm the tap token is accepted.
/// The handshake opens a probe tap and immediately closes it with zero bytes, so
/// the Recorder discards the empty recording (no session side effect).
/// </summary>
public static class ConnectionTester
{
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
