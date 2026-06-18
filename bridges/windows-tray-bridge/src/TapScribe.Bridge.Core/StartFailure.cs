using System.Net;

namespace TapScribe.Bridge.Core;

/// <summary>Why a meeting failed to start, as far as the operator needs to care.</summary>
public enum StartFailureKind
{
    /// <summary>No response from the Recorder — wrong host/port, or it isn't running.</summary>
    Unreachable,

    /// <summary>The Recorder answered but refused the tap token (401/403).</summary>
    TokenRejected,

    /// <summary>Anything else — surfaced with the raw error message.</summary>
    Other,
}

/// <summary>
/// A classified, user-facing reason a Start meeting attempt failed. The tray shell
/// runs <see cref="Classify"/> on whatever the pre-start detached-session POST threw,
/// so it can distinguish "your token was rejected" from "the Recorder is unreachable"
/// instead of surfacing a raw "status code 401". Pure, so it is unit-tested here with
/// constructed exceptions (no live Recorder).
/// </summary>
public sealed record StartFailure(StartFailureKind Kind, string Message)
{
    public static StartFailure Classify(Exception error, string host, int port)
    {
        ArgumentNullException.ThrowIfNull(error);

        if (error is HttpRequestException { StatusCode: HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden })
            return new StartFailure(
                StartFailureKind.TokenRejected,
                "Tap token rejected by the Recorder. Check the token in Settings.");

        // A request that timed out (or was otherwise cancelled) never got a response —
        // same operator meaning as unreachable.
        if (error is OperationCanceledException)
            return new StartFailure(
                StartFailureKind.Unreachable,
                $"Recorder did not respond within the timeout at {host}:{port}. Is it running?");

        // No StatusCode means no HTTP response was received — a connection-level
        // failure (wrong host/port, Recorder not running, TLS handshake refused).
        if (error is HttpRequestException { StatusCode: null })
            return new StartFailure(
                StartFailureKind.Unreachable,
                $"Recorder unreachable at {host}:{port}. Is it running, and are the host/port/TLS correct?");

        return new StartFailure(StartFailureKind.Other, error.Message);
    }
}
