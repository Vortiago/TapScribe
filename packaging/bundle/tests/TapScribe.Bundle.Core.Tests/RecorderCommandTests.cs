using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// Tests for <see cref="RecorderCommand"/> — the Bundle's two child processes as DATA.
/// Same convention as the repo's <c>live.build_live_cmd</c>: a pure argv builder, no
/// spawn, so the command surface is asserted here rather than observed in production.
/// </summary>
public class RecorderCommandTests
{
    private static readonly BundleLayout Layout = BundleLayout.ForWindows("/opt/prog", "/home/op");
    private const string Wheel = "/opt/prog/wheel/tapscribe-1.0.0-py3-none-any.whl";

    [Fact]
    public void Preflight_RunsTheConsoleInterpreterAsAModule()
    {
        BundleProcess cmd = RecorderCommand.Preflight(Layout, Wheel);

        // python.exe, NOT pythonw.exe: preflight is blocking and its output is logged,
        // so we want a console interpreter with real stdout/stderr.
        Assert.Equal(Layout.Python, cmd.Executable);
        Assert.Equal(new[] { "-m", "tapscribe.preflight", "--install-spec", Wheel }, cmd.Arguments);
    }

    [Fact]
    public void Recorder_RunsTheWindowlessInterpreterAsAModule()
    {
        BundleProcess cmd = RecorderCommand.Recorder(Layout, Wheel);

        // pythonw.exe: the Recorder is long-lived and must not flash a console window.
        Assert.Equal(Layout.Pythonw, cmd.Executable);
        Assert.Equal(new[] { "-m", "tapscribe", "--install-spec", Wheel }, cmd.Arguments);
    }

    [Fact]
    public void BothCommands_PointTheRecorderAtTheOperatorDataDirectory()
    {
        foreach (BundleProcess cmd in new[] { RecorderCommand.Preflight(Layout, Wheel), RecorderCommand.Recorder(Layout, Wheel) })
        {
            Assert.Equal(Layout.DataDirectory, cmd.Environment["TAPSCRIBE_BASE_DIR"]);
            // start.ps1 set this and preflight.py inherited the job (ADR-0015): an
            // unbuffered child is the difference between a live log and a log that
            // only appears when the process dies.
            Assert.Equal("1", cmd.Environment["PYTHONUNBUFFERED"]);
        }
    }

    [Fact]
    public void Recorder_PassesTheWheelAsAnAbsolutePath()
    {
        // pip runs with a different cwd than the tray; install_target absolutises
        // the spec, but handing it a relative path would make the tray's own
        // "wheel not found" diagnostics depend on cwd.
        BundleProcess cmd = RecorderCommand.Recorder(Layout, Wheel);

        string spec = cmd.Arguments[cmd.Arguments.Count - 1];
        Assert.True(Path.IsPathRooted(spec));
    }

    [Fact]
    public void Commands_RejectABlankWheelSpec()
    {
        Assert.Throws<ArgumentException>(() => RecorderCommand.Preflight(Layout, "  "));
        Assert.Throws<ArgumentException>(() => RecorderCommand.Recorder(Layout, ""));
    }

    [Fact]
    public void Arguments_AreDataNotAShellString()
    {
        // CLAUDE.md: subprocess argv is always the list form — never an f-string,
        // never shell=True. Each token stands alone even when it contains spaces.
        BundleProcess cmd = RecorderCommand.Recorder(
            BundleLayout.ForWindows("/opt/Program Files/TapScribe", "/home/op"),
            "/opt/Program Files/TapScribe/wheel/tapscribe-1.0.0-py3-none-any.whl");

        Assert.Equal(4, cmd.Arguments.Count);
        Assert.Contains(" ", cmd.Arguments[3], StringComparison.Ordinal);
    }

    [Fact]
    public void DashboardUrl_IsTheRecordersLoopbackPort()
    {
        // The host role only ever opens the LOCAL dashboard — a Bundle is a Recorder on
        // this machine (ADR-0015). 8001 is config.py's port.
        Assert.Equal("http://localhost:8001/", BundleDefaults.DashboardUrl);
    }
}
