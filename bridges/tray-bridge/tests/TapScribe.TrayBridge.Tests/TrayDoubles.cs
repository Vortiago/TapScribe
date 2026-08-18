using System.Text;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// The tray's notification-area presence, recorded instead of registered. This is the ONE
/// double that exists for the host rather than for the assertions: a real
/// <c>NotifyIcon</c> with <c>Visible = true</c> calls <c>Shell_NotifyIcon</c>, which on a
/// runner with no interactive shell to answer it blocks for seconds and then took the test
/// host down with it (PR #428's first run: the whole assembly aborted before a single result,
/// with no assertion and no stack). Nothing under test needs an icon in the notification
/// area; the status the icon WOULD show is asserted through the menu's header line, which is
/// a plain object.
///
/// The two notice channels are recorded apart, because which one a notice takes is the thing
/// the shell decides: a warning and an information balloon differ in the operator's reading of
/// them, and the mapping from <see cref="NoticeKind"/> is the shell's whole contribution.
/// </summary>
internal sealed class FakeIndicator : ITrayIndicator
{
    public ContextMenuStrip? AttachedMenu { get; private set; }
    public StatusView? LastStatus { get; private set; }
    public List<(string Title, string Message)> Warnings { get; } = [];
    public List<(string Title, string Message)> Informations { get; } = [];
    public bool Disposed { get; private set; }

    public void Attach(ContextMenuStrip menu) => AttachedMenu = menu;

    public void Show(StatusView view) => LastStatus = view;

    public void Warn(string title, string message) => Warnings.Add((title, message));

    public void Inform(string title, string message) => Informations.Add((title, message));

    public void Dispose() => Disposed = true;
}

/// <summary>A device tree with nothing in it. The shell no longer touches devices at all (the
/// meeting lifecycle is <see cref="BridgeRuntime"/>'s, and covered there), so this exists only
/// to complete the runtime's dependencies without a WASAPI endpoint anywhere near the
/// runner.</summary>
internal sealed class NoDevices : IAudioDeviceEnumerator
{
    public IReadOnlyList<CaptureDevice> List() => [];

    public IAudioCapture Open(CaptureDevice device) =>
        throw new InvalidOperationException("no devices are registered in the tray shell's tests");

    public void Dispose()
    {
        // Nothing native behind this, and no tray test asks whether it was released: the
        // ownership rules for a real enumerator are the orchestrator's, and pinned in
        // CaptureOwnershipTests.
    }
}

/// <summary>The settings store's at-rest translation, with nothing at rest: the tray tests
/// never persist a token, and DPAPI is the operator's real secret store rather than a
/// runner's.</summary>
internal sealed class PlainTokenStore : ITapTokenStore
{
    public string? Write(string token) =>
        string.IsNullOrEmpty(token) ? null : Convert.ToBase64String(Encoding.UTF8.GetBytes(token));

    public string Read(string? atRest) =>
        string.IsNullOrEmpty(atRest) ? "" : Encoding.UTF8.GetString(Convert.FromBase64String(atRest));
}

/// <summary>
/// The shell's whole outside world, scripted: the indicator it renders through, the three
/// stores pointed at a temp directory rather than the operator's %APPDATA%, and the loop-start
/// kick held so a test can decide when the message loop's first turn happens.
/// </summary>
internal sealed class TrayHarness : IDisposable
{
    private readonly string _directory =
        Path.Join(Path.GetTempPath(), "tapscribe-tray-" + Guid.NewGuid().ToString("N"));

    private readonly List<Action> _kicks = [];

    public FakeIndicator Indicator { get; } = new();

    /// <summary>What the shell scheduled for the message loop's first turn. <see cref="StaShell.Build"/>
    /// runs these, which is what the loop would do; a test that wants to watch the deferral
    /// itself supplies its own scheduling instead.</summary>
    public IReadOnlyList<Action> Kicks => _kicks;

    public MeetingStateStore StateStore => _stateStore ??= new MeetingStateStore(_directory);

    private MeetingStateStore? _stateStore;

    public MeetingHistoryStore HistoryStore => _historyStore ??= new MeetingHistoryStore(_directory);

    private MeetingHistoryStore? _historyStore;

    public BridgeSettingsStore SettingsStore =>
        _settingsStore ??= new BridgeSettingsStore(
            new PlainTokenStore(), _directory, "tray-test.json", TrayStores.FallbackIdentity);

    private BridgeSettingsStore? _settingsStore;

    public BridgeSettings Settings { get; init; } = DefaultSettings();

    /// <summary>The settings every tray test starts from, in ONE spelling so a variant can
    /// change the one field it cares about instead of re-typing the rest. They point at a port
    /// nothing listens on: a loopback connect there is refused immediately, so any request a
    /// resumed flow DOES make fails fast and deterministically, with no server, no timeout and
    /// no wall-clock in any assertion.</summary>
    public static BridgeSettings DefaultSettings() => new()
    {
        Host = "127.0.0.1",
        Port = 9, // discard/unassigned: connection refused, instantly
        Identity = "alice",
        Name = "Alice",
        Devices = [],
    };

    /// <summary>The shell's outside world, built once. A fresh record per read would hand
    /// StaShell.Build and a test that reads this directly DIFFERENT instances over the same
    /// harness: the allocation is trivial, the footgun is not.</summary>
    public TrayDependencies Dependencies => _dependencies ??= BuildDependencies();

    private TrayDependencies? _dependencies;

    private TrayDependencies BuildDependencies() => new(
        new BridgeDependencies(
            static () => new NoDevices(),
            static (_, _) => Task.FromResult("2026-08-10T09-00-00"),
            SettingsStore,
            StateStore,
            HistoryStore),
        () => Indicator,
        _kicks.Add);

    public void Dispose()
    {
        try
        {
            if (Directory.Exists(_directory))
                Directory.Delete(_directory, recursive: true);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            // Teardown of a temp directory, running from a using-block where a test may already
            // be reporting its own failure: a file a resumed flow still holds open must not
            // throw over the top of the result the reader needs. What is lost is one temp
            // directory, which the OS reclaims.
        }
    }
}
