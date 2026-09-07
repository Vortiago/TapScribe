namespace TapScribe.Bundle.Core;

/// <summary>
/// A Bundle's on-disk shape is wrong in a way the operator must see — most often a
/// packaging bug (no wheel shipped, or two).
/// </summary>
public sealed class BundleLayoutException : Exception
{
    public BundleLayoutException(string message) : base(message) { }
}

/// <summary>Which platform's Bundle this is. The two differ in three ways and no more:
/// where the payload ships, where pip may write, and what the interpreter is called.</summary>
public enum BundleShape
{
    Windows,
    MacOS,
}

/// <summary>
/// Where everything in a <b>Bundle</b> lives (ADR-0015, ADR-0024), resolved from the
/// directory the tray ships in and the operator's home.
///
/// Three roots, and the split between the first two is the whole of what macOS added:
/// <list type="bullet">
///   <item><b>Payload</b> — the embedded CPython and the shipped <c>tapscribe-*.whl</c>,
///   AS SHIPPED. Read-only on macOS (writing inside a signed <c>.app</c> invalidates its
///   signature — ADR-0024), and on Windows simply the folder the exe sits in.</item>
///   <item><b>Runtime</b> — the interpreter pip actually targets: <c>/setup</c>'s model
///   backends and preflight's repairs both install into it. On Windows that IS the
///   payload, which is why the Bundle installs per-user under
///   <c>%LOCALAPPDATA%\Programs\TapScribe</c> rather than into Program Files. On macOS it
///   is a version-stamped COPY under the data root, which <see cref="RuntimeCopy"/>
///   makes.</item>
///   <item><b>Data</b> — what <c>TAPSCRIBE_BASE_DIR</c> points at: recordings, config,
///   <c>.auth-password</c>, and the host role's own logs. Outside the payload so an
///   uninstall cannot delete someone's meetings.</item>
/// </list>
///
/// Everything downstream — <see cref="RecorderCommand"/>, preflight, <c>/setup</c> —
/// reads <see cref="Python"/> and <see cref="WheelDirectory"/> and therefore targets the
/// runtime WITHOUT knowing either platform's story. That is the point of the split: the
/// macOS copy is a fact about this type, not a branch anyone else carries.
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
    /// The interpreter folder inside the payload — and, once copied, inside the runtime: a
    /// relocatable python-build-standalone install with TapScribe's core deps already
    /// pip-installed into it, which <c>/setup</c> then installs model backends into at
    /// runtime.
    /// </summary>
    public const string PythonFolder = "python";

    /// <summary>The folder carrying the single shipped wheel.</summary>
    public const string WheelFolder = "wheel";

    /// <summary>The data dir's name under the operator's home — also the Windows
    /// installer's app name and the macOS bundle's folder under Application Support.</summary>
    public const string DataFolder = "TapScribe";

    /// <summary>The host role's log folder inside the data dir.</summary>
    public const string LogFolder = "logs";

    /// <summary>The Recorder's session output folder inside the data dir.</summary>
    public const string RecordingsFolder = "recordings";

    /// <summary>The active log file. <see cref="LogRotation"/> derives archive names from it.</summary>
    public const string LogFileName = "recorder.log";

    /// <summary>The Recorder's dashboard password file (<c>config.AUTH_PASSWORD_FILE</c>).</summary>
    public const string PasswordFileName = ".auth-password";

    /// <summary>Holds the macOS runtime copies, one folder per version (ADR-0024).</summary>
    public const string RuntimeFolder = "runtime";

    /// <summary>Where a macOS <c>.app</c> carries read-only payload, and where the
    /// <c>.pkg</c> stages the interpreter and the wheel. Named once, because three things
    /// have to agree on it: this layout, the tray's role probe, and the package script.</summary>
    public const string ResourcesFolder = "Resources";

    /// <summary>The <c>.app</c> subfolder every bundle has, which
    /// <see cref="MacOSPayload"/> climbs to.</summary>
    private const string ContentsFolder = "Contents";

    /// <summary>The interpreter's path RELATIVE to the python folder, and the GUI one beside
    /// it. Carried as state rather than switched on <see cref="Shape"/> at each use: the two
    /// factories already know the platform, so a third shape then has to supply its answers
    /// to the constructor — where the compiler asks — instead of falling through a
    /// two-armed branch to whichever side was written second.</summary>
    private readonly string _python;
    private readonly string _pythonw;
    private readonly string _reinstallAdvice;

    private BundleLayout(
        BundleShape shape,
        string payloadDirectory,
        string dataDirectory,
        string runtimeDirectory,
        string python,
        string pythonw,
        string reinstallAdvice)
    {
        Shape = shape;
        PayloadDirectory = payloadDirectory;
        DataDirectory = dataDirectory;
        RuntimeDirectory = runtimeDirectory;
        _python = python;
        _pythonw = pythonw;
        _reinstallAdvice = reinstallAdvice;
    }

    /// <summary>Which platform's Bundle this is.</summary>
    public BundleShape Shape { get; }

    /// <summary>Where the payload ships: beside the tray exe on Windows, inside the
    /// <c>.app</c> on macOS. The <see cref="HostPayloadPresent"/> probe's target, and the
    /// SOURCE a macOS runtime copy reads from.</summary>
    public string PayloadDirectory { get; }

    /// <summary>What <c>TAPSCRIBE_BASE_DIR</c> is set to.</summary>
    public string DataDirectory { get; }

    /// <summary>The interpreter root every pip run targets. Equal to
    /// <see cref="PayloadDirectory"/> on Windows; a version-stamped copy under
    /// <see cref="DataDirectory"/> on macOS.</summary>
    public string RuntimeDirectory { get; }

    /// <summary>Whether the runtime is a copy that has to exist before anything can run.
    /// Read off the shape rather than re-derived by comparing the two roots: one fact in two
    /// encodings can disagree, and this is the first thing <see cref="RuntimeCopy"/> asks.</summary>
    public bool RuntimeIsACopy => Shape is BundleShape.MacOS;

    public string PythonDirectory => Path.Join(RuntimeDirectory, PythonFolder);

    /// <summary>
    /// Console interpreter — used for blocking, logged steps like preflight.
    ///
    /// At the ROOT of the interpreter folder on Windows, not under <c>Scripts\</c>: the
    /// Bundle ships a python-build-standalone <c>install_only</c> distribution and
    /// pip-installs into it directly, so there is no venv and no <c>Scripts\python.exe</c>.
    /// (<c>Scripts\</c> exists, but holds console-script entry points like <c>pip.exe</c>
    /// and <c>whisperlivekit-server.exe</c>.) The same distribution on macOS is
    /// POSIX-shaped, so the interpreter is <c>bin/python3</c> and the entry points are
    /// beside it in <c>bin/</c>.
    /// </summary>
    public string Python => Path.Join(PythonDirectory, _python);

    /// <summary>The interpreter for the long-lived Recorder: windowless on Windows, so no
    /// console flashes and none stays open, and the same binary as <see cref="Python"/> on
    /// macOS (see <see cref="ForMacOS"/>).</summary>
    public string Pythonw => Path.Join(PythonDirectory, _pythonw);

    public string WheelDirectory => Path.Join(RuntimeDirectory, WheelFolder);

    /// <summary>The interpreter as SHIPPED — the copy's source. Only
    /// <see cref="RuntimeCopy"/> has business with it.</summary>
    public string PayloadPythonDirectory => Path.Join(PayloadDirectory, PythonFolder);

    /// <summary>The wheel as SHIPPED — the copy's source.</summary>
    public string PayloadWheelDirectory => Path.Join(PayloadDirectory, WheelFolder);

    /// <summary>Where the version-stamped runtime copies live. Meaningless on Windows,
    /// whose runtime is the payload — the path resolves there, nothing reads it.</summary>
    public string RuntimeRoot => Path.Join(DataDirectory, RuntimeFolder);

    public string PasswordFile => Path.Join(DataDirectory, PasswordFileName);

    /// <summary>Where the Recorder writes sessions (<c>config.RECORDINGS_DIR</c>). Named
    /// here with every other folder rather than in a shell: the macOS tray reveals it in
    /// Finder, and a literal there is the one folder name no CI leg would compile.</summary>
    public string RecordingsDirectory => Path.Join(DataDirectory, RecordingsFolder);

    public string LogDirectory => Path.Join(DataDirectory, LogFolder);

    public string LogFile => Path.Join(LogDirectory, LogFileName);

    /// <summary>What to tell an operator whose install is broken. Platform-specific because
    /// the two are repaired by downloading different files, and "reinstall from
    /// TapScribe-Setup-win-x64.exe" on a Mac is worse than saying nothing.</summary>
    public string ReinstallAdvice => _reinstallAdvice;

    /// <summary>
    /// Resolve the Windows layout from the tray's own directory and the user profile.
    /// Both are made absolute: pip and the Recorder run with a different cwd than the
    /// tray, so a relative path handed onward would silently follow theirs.
    ///
    /// Payload and runtime are the SAME folder here, which is exactly why the Windows
    /// Bundle installs per-user: <c>/setup</c> pip-installs into the shipped interpreter,
    /// so it has to be writable without elevation (ADR-0015).
    /// </summary>
    public static BundleLayout ForWindows(string programDirectory, string userProfileDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(programDirectory);
        ArgumentException.ThrowIfNullOrWhiteSpace(userProfileDirectory);

        string payload = Path.GetFullPath(programDirectory);
        return new BundleLayout(
            BundleShape.Windows,
            payload,
            Path.GetFullPath(Path.Join(userProfileDirectory, DataFolder)),
            payload,
            python: "python.exe",
            pythonw: "pythonw.exe",
            reinstallAdvice: "reinstall from a freshly downloaded TapScribe-Setup-win-x64.exe.");
    }

    /// <summary>
    /// Resolve the macOS layout: read-only payload inside the <c>.app</c>, data under
    /// <c>~/Library/Application Support/TapScribe</c>, and the runtime a copy stamped with
    /// the <c>.app</c>'s version (ADR-0024).
    ///
    /// The version is passed rather than read here because <c>Bundle.Core</c> has no
    /// bundle to read it from — the shell knows its own, and a Core that guessed would be
    /// untestable on the Linux leg that covers everything else in this type.
    /// </summary>
    /// <param name="payloadDirectory">Normally <see cref="MacOSPayload"/> of the running
    /// assembly's directory.</param>
    /// <param name="homeDirectory">The operator's home, NOT the data root.</param>
    /// <param name="version">The <c>.app</c>'s version. An upgrade changes it, which is
    /// what makes the runtime re-copy rather than serve the previous release's wheel.</param>
    public static BundleLayout ForMacOS(string payloadDirectory, string homeDirectory, string version)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(payloadDirectory);
        ArgumentException.ThrowIfNullOrWhiteSpace(homeDirectory);
        ArgumentException.ThrowIfNullOrWhiteSpace(version);

        // Path.Join's multi-arg overload, so the two macOS segments need no separator
        // literal and the Core stays testable off macOS.
        string data = Path.GetFullPath(
            Path.Join(homeDirectory, "Library", "Application Support", DataFolder));
        // The same binary for both: macOS has no pythonw twin — a child of a GUI app gets no
        // terminal to begin with — so this is deliberately not a second name that would have
        // to exist. Callers do not branch; that is why the property survives on both.
        string interpreter = Path.Join("bin", "python3");
        return new BundleLayout(
            BundleShape.MacOS,
            Path.GetFullPath(payloadDirectory),
            data,
            Path.Join(data, RuntimeFolder, SafeStamp(version)),
            python: interpreter,
            pythonw: interpreter,
            reinstallAdvice: "reinstall from a freshly downloaded TapScribe-Bundle-osx-arm64.pkg.");
    }

    /// <summary>
    /// The read-only payload folder inside a macOS <c>.app</c>, from the directory the
    /// running assemblies sit in (<c>Contents/MonoBundle</c> for a <c>net*-macos</c> app).
    ///
    /// Climbs to <c>Contents</c> rather than assuming <c>MonoBundle</c>: the SDK has moved
    /// managed assemblies between <c>Contents/MonoBundle</c> and <c>Contents/MacOS</c>
    /// before, and a wrong guess here reads downstream as "no host payload" — a Bundle
    /// silently demoted to a bridge-only tray, which is the same failure
    /// <see cref="HostPayloadPresent"/>'s OR exists to prevent.
    ///
    /// Answers a path that simply will not exist when there is no <c>Contents</c> above
    /// (a <c>dotnet run</c>, a test host) rather than throwing: the caller's very next
    /// step is the role probe, and "no" is the right answer there.
    /// </summary>
    public static string MacOSPayload(string baseDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(baseDirectory);

        string full = Path.GetFullPath(baseDirectory);
        for (DirectoryInfo? dir = new(full); dir is not null; dir = dir.Parent)
        {
            if (string.Equals(dir.Name, ContentsFolder, StringComparison.Ordinal))
                return Path.Join(dir.FullName, ResourcesFolder);
        }

        return Path.Join(full, ResourcesFolder);
    }

    /// <summary>
    /// A version reduced to what may safely be ONE path segment. The stamp becomes a
    /// directory name under the data root, and the version reaching a macOS build is
    /// whatever <c>-p:Version=</c> was given — a tag, and therefore external text. Anything
    /// that is not alphanumeric, dot, dash or underscore becomes a dash, so no separator
    /// and no <c>..</c> can steer the copy out of <see cref="RuntimeRoot"/>.
    /// </summary>
    private static string SafeStamp(string version)
    {
        char[] safe = version.Select(
            c => char.IsAsciiLetterOrDigit(c) || c is '.' or '-' or '_' ? c : '-').ToArray();
        string stamp = new string(safe).Trim('.', '-');
        return stamp.Length == 0 ? "unknown" : stamp;
    }

    /// <summary>
    /// Whether a host payload sits beside the tray on disk — the HOST ROLE test
    /// (ADR-0022). It is a fact about the install, not a flag, a build variant or a
    /// setting anyone can misconfigure: the same tray executable ships in the bridge-only
    /// artifact and in the Bundle, and this is the whole difference between them.
    ///
    /// Asked of the PAYLOAD, never the runtime: on macOS the runtime does not exist until
    /// the first launch has copied it, so a runtime-based probe would answer "bridge-only"
    /// to every Bundle exactly once — on the launch that was supposed to do the copying.
    ///
    /// Deliberately NOT "does <see cref="ForWindows"/> succeed" — that is pure path
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
    public static bool HostPayloadPresent(string payloadDirectory)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(payloadDirectory);
        string root = Path.GetFullPath(payloadDirectory);
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
                $"incomplete — {ReinstallAdvice}");

        string[] wheels = Directory.GetFiles(dir, "*.whl", SearchOption.TopDirectoryOnly);
        Array.Sort(wheels, StringComparer.Ordinal);

        if (wheels.Length == 1)
            return Path.GetFullPath(wheels[0]);

        if (wheels.Length == 0)
            throw new BundleLayoutException(
                $"no TapScribe wheel found in {dir}. This build of TapScribe is incomplete — " +
                ReinstallAdvice);

        string names = string.Join(", ", wheels.Select(Path.GetFileName));
        throw new BundleLayoutException(
            $"expected exactly one TapScribe wheel in {dir} but found {wheels.Length}: {names}. " +
            $"Refusing to guess which version to install — remove the stale one, or {ReinstallAdvice}");
    }
}
