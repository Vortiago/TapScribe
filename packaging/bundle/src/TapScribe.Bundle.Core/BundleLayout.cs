namespace TapScribe.Bundle.Core;

/// <summary>
/// A Bundle's on-disk shape is wrong in a way the operator must see — most often a
/// packaging bug (no wheel shipped, or two).
/// </summary>
public sealed class BundleLayoutException : Exception
{
    public BundleLayoutException(string message) : base(message) { }
}

/// <summary>
/// Where everything in a Windows <b>Bundle</b> lives (ADR-0015), resolved from just two
/// inputs: the directory the tray exe sits in, and the operator's user profile.
///
/// Two roots, deliberately separate:
/// <list type="bullet">
///   <item><b>Program</b> (<c>%LOCALAPPDATA%\Programs\TapScribe</c>) — the embedded
///   CPython and the shipped <c>tapscribe-*.whl</c>. Per-user, because <c>/setup</c>
///   pip-installs into that interpreter at runtime and so it must be writable without
///   elevation.</item>
///   <item><b>Data</b> (<c>%USERPROFILE%\TapScribe</c>) — what
///   <c>TAPSCRIBE_BASE_DIR</c> points at: recordings, config, <c>.auth-password</c>, and
///   the host role's own logs. Outside the program dir so an uninstall cannot delete
///   someone's meetings.</item>
/// </list>
///
/// Path construction only — the sole disk access is <see cref="ResolveWheel"/>, which
/// has to look at what actually shipped. Everything uses <see cref="Path.Join"/>
/// rather than <c>Path.Combine</c> and rather than literal separators. Combine
/// silently DISCARDS everything before a rooted later argument, turning a bad input
/// into a plausible-looking path somewhere else on disk instead of an error; Join
/// just concatenates. Avoiding literal separators is what lets the Core stay
/// cross-platform and be tested on the Linux CI leg.
/// </summary>
public sealed record BundleLayout
{
    /// <summary>
    /// The interpreter folder inside the program dir: a relocatable
    /// python-build-standalone install with TapScribe's core deps already pip-installed
    /// into it, which <c>/setup</c> then installs model backends into at runtime.
    /// </summary>
    public const string PythonFolder = "python";

    /// <summary>The folder inside the program dir carrying the single shipped wheel.</summary>
    public const string WheelFolder = "wheel";

    /// <summary>The data dir's name under the user profile — also the Windows installer's app name.</summary>
    public const string DataFolder = "TapScribe";

    /// <summary>The host role's log folder inside the data dir.</summary>
    public const string LogFolder = "logs";

    /// <summary>The active log file. <see cref="LogRotation"/> derives archive names from it.</summary>
    public const string LogFileName = "recorder.log";

    /// <summary>The Recorder's dashboard password file (<c>config.AUTH_PASSWORD_FILE</c>).</summary>
    public const string PasswordFileName = ".auth-password";

    private BundleLayout(string programDirectory, string dataDirectory)
    {
        ProgramDirectory = programDirectory;
        DataDirectory = dataDirectory;
    }

    /// <summary>Where the tray exe lives; carries the interpreter and the wheel.</summary>
    public string ProgramDirectory { get; }

    /// <summary>What <c>TAPSCRIBE_BASE_DIR</c> is set to.</summary>
    public string DataDirectory { get; }

    public string PythonDirectory => Path.Join(ProgramDirectory, PythonFolder);

    /// <summary>
    /// Console interpreter — used for blocking, logged steps like preflight.
    ///
    /// At the ROOT of the interpreter folder, not under <c>Scripts\</c>: the Bundle
    /// ships a python-build-standalone <c>install_only</c> distribution installed to
    /// <c>{app}\python</c> and pip-installs into it directly, so there is no venv and no
    /// <c>Scripts\python.exe</c>. (<c>Scripts\</c> exists, but holds console-script entry
    /// points like <c>pip.exe</c> and <c>whisperlivekit-server.exe</c>.)
    /// </summary>
    public string Python => Path.Join(PythonDirectory, "python.exe");

    /// <summary>Windowless interpreter — used for the long-lived Recorder so no console flashes.</summary>
    public string Pythonw => Path.Join(PythonDirectory, "pythonw.exe");

    public string WheelDirectory => Path.Join(ProgramDirectory, WheelFolder);

    public string PasswordFile => Path.Join(DataDirectory, PasswordFileName);

    public string LogDirectory => Path.Join(DataDirectory, LogFolder);

    public string LogFile => Path.Join(LogDirectory, LogFileName);

    /// <summary>
    /// Resolve the layout from the tray's own directory and the user profile.
    /// Both are made absolute: pip and the Recorder run with a different cwd than the
    /// tray, so a relative path handed onward would silently follow theirs.
    /// </summary>
    public static BundleLayout Resolve(string programDirectory, string userProfileDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(programDirectory);
        ArgumentException.ThrowIfNullOrWhiteSpace(userProfileDirectory);

        return new BundleLayout(
            Path.GetFullPath(programDirectory),
            Path.GetFullPath(Path.Join(userProfileDirectory, DataFolder)));
    }

    /// <summary>
    /// Whether a host payload sits beside the tray on disk — the HOST ROLE test
    /// (ADR-0022). It is a fact about the install, not a flag, a build variant or a
    /// setting anyone can misconfigure: the same tray executable ships in the bridge-only
    /// artifact and in the Bundle, and this is the whole difference between them.
    ///
    /// Deliberately NOT "does <see cref="Resolve"/> succeed" — that is pure path
    /// construction and probes nothing — and NOT "does <see cref="ResolveWheel"/> return"
    /// — that is designed to THROW on a packaging bug the operator must see. Folded into a
    /// boolean role probe, a Bundle whose <c>wheel/</c> was wiped would silently degrade
    /// to a bridge-only tray and the operator's Recorder would just vanish from the menu.
    ///
    /// EITHER folder claims the role, not both: an <c>AND</c> is exactly the silent
    /// demotion above, and a <c>python/</c>-only probe is the same failure mirrored for a
    /// wiped interpreter. Claim the role whenever any payload is there, and let
    /// <see cref="ResolveWheel"/>'s errors — and a missing interpreter — stay loud INSIDE
    /// it, which is where they belong.
    /// </summary>
    public static bool HostPayloadPresent(string programDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(programDirectory);
        string root = Path.GetFullPath(programDirectory);
        return Directory.Exists(Path.Join(root, PythonFolder))
            || Directory.Exists(Path.Join(root, WheelFolder));
    }

    /// <summary>
    /// The absolute path of the one <c>*.whl</c> the Bundle ships, for
    /// <c>--install-spec</c>.
    ///
    /// Zero or several is a <b>packaging</b> bug and throws. There is deliberately no
    /// "pick the newest" fallback: a Bundle whose installer version silently diverges
    /// from the wheel it installs is the exact drift the bundled-wheel decision exists
    /// to prevent (ADR-0015), and a silent pick would hide it until someone wondered why
    /// the dashboard reported an older version than they installed.
    /// </summary>
    public string ResolveWheel()
    {
        string dir = WheelDirectory;
        if (!Directory.Exists(dir))
            throw new BundleLayoutException(
                $"the Bundle's wheel folder is missing: {dir}. This build of TapScribe is " +
                "incomplete — reinstall from a freshly downloaded TapScribe-Setup-win-x64.exe.");

        string[] wheels = Directory.GetFiles(dir, "*.whl", SearchOption.TopDirectoryOnly);
        Array.Sort(wheels, StringComparer.Ordinal);

        if (wheels.Length == 1)
            return Path.GetFullPath(wheels[0]);

        if (wheels.Length == 0)
            throw new BundleLayoutException(
                $"no TapScribe wheel found in {dir}. This build of TapScribe is incomplete — " +
                "reinstall from a freshly downloaded TapScribe-Setup-win-x64.exe.");

        string names = string.Join(", ", wheels.Select(Path.GetFileName));
        throw new BundleLayoutException(
            $"expected exactly one TapScribe wheel in {dir} but found {wheels.Length}: {names}. " +
            "Refusing to guess which version to install — remove the stale one, or reinstall " +
            "from a freshly downloaded TapScribe-Setup-win-x64.exe.");
    }
}
