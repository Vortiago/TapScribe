using System.Collections.ObjectModel;

namespace TapScribe.Bundle.Core;

/// <summary>Constants the host role shares with the Recorder it boots.</summary>
public static class BundleDefaults
{
    /// <summary>The Recorder's HTTP port (<c>config.py</c>).</summary>
    public const int RecorderPort = 8001;

    /// <summary>
    /// What "Open dashboard" navigates to. Always loopback: a Bundle <i>is</i> a Recorder
    /// on this machine (ADR-0015), and the <c>--lan</c>/<c>--tls</c> topologies are about
    /// other machines reaching in, not about where this tray points.
    /// </summary>
    public const string DashboardUrl = "http://localhost:8001/";
}

/// <summary>
/// One child process the Launcher runs, as data: the executable, its argv (list form,
/// never a shell string — CLAUDE.md), and the environment overlay applied on top of the
/// Launcher's own environment.
/// </summary>
public sealed record BundleProcess(
    string Executable,
    IReadOnlyList<string> Arguments,
    IReadOnlyDictionary<string, string> Environment);

/// <summary>
/// Builds the Bundle's two child commands. Pure — no spawn, no environment mutation —
/// following the same convention as the repo's <c>live.build_live_cmd</c>: the CLI
/// surface exists as data so it can be asserted in a unit test instead of discovered in
/// production.
///
/// Both commands carry <c>--install-spec &lt;wheel&gt;</c>. That flag is how a Bundle
/// tells the Recorder it is neither a checkout nor a PyPI install, so <c>/setup</c>'s
/// pip runs resolve extras from the shipped wheel's own metadata (ADR-0015). The wheel
/// path is passed absolute; <c>install_target.resolve_install_spec</c> absolutises it
/// again, but pip runs with a different cwd and a relative spec would make the failure
/// mode cwd-dependent.
/// </summary>
public static class RecorderCommand
{
    /// <summary>Env var the Recorder reads for its data root (<c>config.BASE_DIR</c>).</summary>
    public const string BaseDirVariable = "TAPSCRIBE_BASE_DIR";

    /// <summary>
    /// <c>&lt;python.exe&gt; -m tapscribe.preflight --install-spec &lt;wheel&gt;</c>.
    ///
    /// The console interpreter, run to completion before the Recorder starts, with its
    /// output pumped into the Launcher's log. <c>tapscribe.preflight</c> is where
    /// <c>start.ps1</c>'s homeless bring-up steps moved (Windows CUDA torch swap,
    /// silero-vad repair, the <c>[summarize]</c> probe) so a PowerShell copy and a C#
    /// copy cannot drift.
    /// </summary>
    public static BundleProcess Preflight(BundleLayout layout, string wheelPath)
    {
        ArgumentNullException.ThrowIfNull(layout);
        ArgumentException.ThrowIfNullOrWhiteSpace(wheelPath);

        return new BundleProcess(
            layout.Python,
            new[] { "-m", "tapscribe.preflight", "--install-spec", Absolute(wheelPath) },
            EnvironmentFor(layout));
    }

    /// <summary>
    /// <c>&lt;pythonw.exe&gt; -m tapscribe --install-spec &lt;wheel&gt;</c>.
    ///
    /// The windowless interpreter, because the Recorder is long-lived and a console
    /// window would be the app's most visible feature. Its stdout/stderr are still
    /// redirected into the Launcher's log — redirection works fine without a console.
    /// </summary>
    public static BundleProcess Recorder(BundleLayout layout, string wheelPath)
    {
        ArgumentNullException.ThrowIfNull(layout);
        ArgumentException.ThrowIfNullOrWhiteSpace(wheelPath);

        return new BundleProcess(
            layout.Pythonw,
            new[] { "-m", "tapscribe", "--install-spec", Absolute(wheelPath) },
            EnvironmentFor(layout));
    }

    private static string Absolute(string wheelPath) => Path.GetFullPath(wheelPath.Trim());

    /// <summary>
    /// The environment overlay both children get. One overlay rather than two so the
    /// preflight and the Recorder can never disagree about where the data dir is —
    /// preflight repairs the very interpreter the Recorder then runs from.
    /// </summary>
    private static ReadOnlyDictionary<string, string> EnvironmentFor(BundleLayout layout) =>
        new(new Dictionary<string, string>(StringComparer.Ordinal)
        {
            // Neutralise the host's CPython bootstrap variables. A Bundle's whole
            // premise is "needs no Python", so it lands most often on machines where
            // some other Python has been — and PYTHONHOME takes precedence over the
            // executable-relative prefix resolution the embedded interpreter depends
            // on. A stray one aborts python.exe with "init_fs_encoding: failed to get
            // the Python codec of the filesystem encoding" before preflight's first
            // line; a stray PYTHONPATH is subtler, shadowing the bundled torch with
            // the operator's other site-packages. Empty string == removed, because
            // ProcessStartInfo.Environment is seeded from OUR inherited environment.
            ["PYTHONHOME"] = "",
            ["PYTHONPATH"] = "",
            ["PYTHONSTARTUP"] = "",

            [BaseDirVariable] = layout.DataDirectory,
            // start.ps1 set this and it is load-bearing here: an unbuffered child is the
            // difference between a log that updates live and one that only materialises
            // when the process dies — which is precisely when the operator needs it.
            ["PYTHONUNBUFFERED"] = "1",
            // The Recorder prints speaker names and session titles, and `safe_name` is
            // Unicode-aware — "週次会議" survives it verbatim. Under a Bundle stdout is a
            // PIPE, so CPython encodes it with the host ANSI code page (cp1252 on a
            // Western box) using errors='strict': a non-Latin-1 participant name would
            // raise UnicodeEncodeError inside the /tap handler and STOP THE RECORDING.
            // A console never hits this, which is why start.ps1 didn't need it.
            ["PYTHONUTF8"] = "1",
            ["PYTHONIOENCODING"] = "utf-8",
        });
}
