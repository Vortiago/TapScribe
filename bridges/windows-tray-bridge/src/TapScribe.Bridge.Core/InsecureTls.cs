using System.Net.Security;
using System.Security.Cryptography.X509Certificates;

namespace TapScribe.Bridge.Core;

/// <summary>
/// The ONE place that turns off TLS certificate validation, for the opt-in
/// "Allow invalid / self-signed certificate" testing mode (issue #147). Every
/// accessor here returns a validator that accepts <b>any</b> server certificate —
/// expired, wrong-host, or chaining to an untrusted root — which is the
/// <c>curl -k</c> equivalent for reaching a Recorder serving a local self-signed
/// cert. It therefore removes MITM protection entirely and is for local testing
/// only, never against an untrusted network.
///
/// <para>
/// SECURITY: both members below are deliberate <c>cs/disabled-certificate-validation</c>
/// sinks. They are centralised in this single file so the CodeQL alert has exactly
/// one home to review and dismiss as an accepted, reviewed risk on the implementing
/// PR (CLAUDE.md forbids widening the repo-wide query-filters to silence it). Callers
/// MUST gate every use on <c>Tls &amp;&amp; AllowSelfSignedCert</c> so the accept-any
/// validator is never wired up on a normal connection — see
/// <see cref="TapConnectionOptions.AllowSelfSignedCert"/>.
/// </para>
/// </summary>
internal static class InsecureTls
{
    /// <summary>
    /// A <see cref="RemoteCertificateValidationCallback"/> that accepts any server
    /// certificate. Assigned to <c>ClientWebSocketOptions.RemoteCertificateValidationCallback</c>
    /// (the <c>wss://</c> /tap path) only when <c>Tls &amp;&amp; AllowSelfSignedCert</c>.
    /// </summary>
    public static bool AcceptAnyServerCertificate(
        object sender, X509Certificate? certificate, X509Chain? chain, SslPolicyErrors sslPolicyErrors) => true;

    /// <summary>
    /// An owning <see cref="HttpClient"/> whose handler accepts any server certificate,
    /// via the framework's named-dangerous validator. Built ONLY for an owned (not
    /// injected) HttpClient when <c>Tls &amp;&amp; AllowSelfSignedCert</c>; an injected
    /// HttpClient keeps its caller's TLS policy. <c>disposeHandler: true</c> so disposing
    /// the client disposes the handler with it.
    /// </summary>
    public static HttpClient CreateInsecureHttpClient()
    {
        var handler = new HttpClientHandler
        {
            ServerCertificateCustomValidationCallback = HttpClientHandler.DangerousAcceptAnyServerCertificateValidator,
        };
        return new HttpClient(handler, disposeHandler: true);
    }
}
