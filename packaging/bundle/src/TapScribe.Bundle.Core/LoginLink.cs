using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace TapScribe.Bundle.Core;

/// <summary>
/// "Open dashboard", minus the browser: trade the Recorder's password for a single-use login
/// link so the operator lands signed in and the native Basic dialog never appears (ADR-0023).
///
/// Here rather than in the WinForms tray this was written in, for the reason every host-role
/// rule is here: it is HTTP and JSON, not widgets; the macOS Bundle (ADR-0024) is the second
/// shell that needs it; and the ubuntu CI leg runs Bundle.Core, where a shell's code is never
/// executed at all. What stays in the shell is the half that genuinely is the shell's — reading
/// the password file, and handing a URL to the desktop.
///
/// Always the LOCAL Recorder: a tray may legitimately supervise one Recorder and tap into
/// another, and a login link minted against the wrong one is the password sent somewhere it
/// does not belong.
/// </summary>
public static class LoginLink
{
    /// <summary>Long enough for a loopback round-trip on a busy machine, short enough that a
    /// wedged Recorder does not freeze the menu the operator clicked.</summary>
    private static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(5);

    /// <summary>
    /// The URL to open the dashboard with: signed in when the Recorder minted a link, and the
    /// plain dashboard when it could not.
    ///
    /// Never throws, and never surfaces a failure to the operator beyond the log. Falling back
    /// costs them exactly what they had before this feature existed — the password prompt, with
    /// "Copy password" one menu item below — where a balloon out of a click they already know
    /// the outcome of is noise. The Recorder still booting, and a Recorder too old to serve
    /// <c>/api/login-link</c>, both land here.
    /// </summary>
    /// <param name="http">The caller's client; the tray holds one for its lifetime.</param>
    /// <param name="dashboardUrl">Normally <see cref="BundleDefaults.DashboardUrl"/>.</param>
    /// <param name="password">The Recorder's Basic password, read from <c>.auth-password</c>.
    /// Sent per-request rather than on the client, so it cannot ride along on anything else
    /// the tray sends.</param>
    /// <param name="log">Where a failed mint says why. Never given the password.</param>
    /// <summary>
    /// What "Open dashboard" should navigate to for this install: a signed-in link when the
    /// password could be read and the Recorder minted one, and the plain dashboard when
    /// either could not.
    ///
    /// The password READ belongs here with the round-trip, not in each shell. It was written
    /// twice — once per tray — and what was duplicated is not a widget but a policy: which
    /// failures fall back silently, and the anti-leak rule that only the lookup's STATUS is
    /// logged, never text derived from the file (CodeQL
    /// cs/cleartext-storage-of-sensitive-information). A rule about a secret with two
    /// implementations is one edit away from having two behaviours.
    /// </summary>
    public static string DashboardUrlFor(HttpClient http, BundleLayout layout, Action<string> log)
    {
        ArgumentNullException.ThrowIfNull(layout);
        ArgumentNullException.ThrowIfNull(log);

        PasswordLookup lookup = PasswordFile.Read(layout.PasswordFile);
        if (!lookup.IsOk || lookup.Password is null)
        {
            log($"login link: password unavailable ({lookup.Status}) — opening the dashboard signed out.");
            return BundleDefaults.DashboardUrl;
        }

        return SignedInUrl(http, BundleDefaults.DashboardUrl, lookup.Password, log);
    }

    /// <summary>
    /// A target safe to write to a log or show in a balloon: the same URL with any query
    /// stripped.
    ///
    /// Here because the query is THIS type's secret — <c>?k=</c> is a live single-use
    /// dashboard credential (ADR-0023) — and the log it would otherwise reach is one the
    /// tray invites the operator to open and paste. It was written once per shell and the
    /// two had already diverged: one used <c>Uri</c> and preserved <c>file://</c> targets
    /// whole, the other cut at the first '?' and truncated them. Whoever mints the secret
    /// owns what counts as redacting it.
    ///
    /// A non-URL, or a file, is returned unchanged: those carry no secret and the operator
    /// needs the whole path to act on the message.
    /// </summary>
    public static string WithoutSecrets(string? target)
    {
        if (string.IsNullOrEmpty(target))
            return "";

        return Uri.TryCreate(target, UriKind.Absolute, out Uri? uri) && !uri.IsFile && uri.Query.Length > 0
            ? uri.GetLeftPart(UriPartial.Path)
            : target;
    }

    public static string SignedInUrl(
        HttpClient http,
        string dashboardUrl,
        string password,
        Action<string> log,
        TimeSpan? timeout = null)
    {
        ArgumentNullException.ThrowIfNull(http);
        ArgumentNullException.ThrowIfNull(log);

        string root = (dashboardUrl ?? BundleDefaults.DashboardUrl).TrimEnd('/');
        try
        {
            using var deadline = new CancellationTokenSource(timeout ?? DefaultTimeout);
            using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(root + "/api/login-link"))
            {
                Headers =
                {
                    Authorization = new AuthenticationHeaderValue(
                        "Basic",
                        Convert.ToBase64String(
                            Encoding.UTF8.GetBytes($"{BundleDefaults.DashboardUser}:{password}"))),
                },
            };
            using HttpResponseMessage response = http.Send(request, deadline.Token);
            response.EnsureSuccessStatusCode();
            using JsonDocument body = JsonDocument.Parse(
                response.Content.ReadAsStringAsync().GetAwaiter().GetResult());
            // The Recorder answers a PATH, not an absolute URL: it cannot know the host and
            // port this tray reaches it on, and behind a proxy it would guess wrong.
            string? path = body.RootElement.GetProperty("path").GetString();
            return string.IsNullOrEmpty(path) ? root + "/" : root + path;
        }
        // InvalidOperationException and IOException are the shapes a 200 with the WRONG BODY
        // takes: `GetProperty` on a non-object root, `GetString()` on a non-string value, a
        // truncated read. A proxy, a captive portal or a Recorder version mismatch all land
        // there, and without them the "never throws" contract above is not kept — the caller
        // gets a "Something went wrong" balloon and no browser instead of the signed-out
        // dashboard.
        catch (Exception error) when (
            error is HttpRequestException or TaskCanceledException or JsonException
                or KeyNotFoundException or InvalidOperationException or IOException)
        {
            // Only the message, never anything derived from the password
            // (CodeQL cs/cleartext-storage-of-sensitive-information).
            log($"login link: {error.Message} — opening the dashboard signed out.");
            return root + "/";
        }
    }
}
