using System.Net;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// Tests for <see cref="StartFailure"/> — classifying the exception thrown by the
/// pre-start detached-session POST into a user-distinguishable cause, so the tray
/// shell can say "token rejected" vs "Recorder unreachable" BEFORE any device opens
/// (issue #106: a bad token or unreachable Recorder must produce a clear error first).
/// </summary>
public class StartFailureTests
{
    [Fact]
    public void Classify_Http401_IsTokenRejected()
    {
        var error = new HttpRequestException("Unauthorized", inner: null, HttpStatusCode.Unauthorized);

        StartFailure failure = StartFailure.Classify(error, "rec", 8001);

        Assert.Equal(StartFailureKind.TokenRejected, failure.Kind);
        Assert.False(string.IsNullOrWhiteSpace(failure.Message));
    }

    [Fact]
    public void Classify_ConnectionError_IsUnreachable_AndNamesHostPort()
    {
        // A connection-level failure: HttpClient throws with no StatusCode (no HTTP
        // response was ever received).
        var error = new HttpRequestException(HttpRequestError.ConnectionError, "Connection refused");

        StartFailure failure = StartFailure.Classify(error, "rec.local", 9000);

        Assert.Equal(StartFailureKind.Unreachable, failure.Kind);
        Assert.Contains("rec.local", failure.Message, StringComparison.Ordinal);
        Assert.Contains("9000", failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Classify_OperationCanceled_IsUnreachable_AndNamesHostPort()
    {
        // A session-mint that timed out (CancellationTokenSource fired) surfaces as a
        // cancellation — no response was received, so it reads as unreachable.
        StartFailure failure = StartFailure.Classify(new OperationCanceledException(), "rec.local", 9000);

        Assert.Equal(StartFailureKind.Unreachable, failure.Kind);
        Assert.Contains("rec.local", failure.Message, StringComparison.Ordinal);
        Assert.Contains("9000", failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void Classify_OtherHttpError_IsOther_SurfacingTheRawMessage()
    {
        var error = new HttpRequestException("Internal Server Error", inner: null, HttpStatusCode.InternalServerError);

        StartFailure failure = StartFailure.Classify(error, "rec", 8001);

        Assert.Equal(StartFailureKind.Other, failure.Kind);
        Assert.Contains("Internal Server Error", failure.Message, StringComparison.Ordinal);
    }
}
