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
                        Convert.ToBase64String(Encoding.UTF8.GetBytes($"admin:{password}"))),
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
        catch (Exception error) when (
            error is HttpRequestException or TaskCanceledException or JsonException or KeyNotFoundException)
        {
            // Only the message, never anything derived from the password
            // (CodeQL cs/cleartext-storage-of-sensitive-information).
            log($"login link: {error.Message} — opening the dashboard signed out.");
            return root + "/";
        }
    }
}
