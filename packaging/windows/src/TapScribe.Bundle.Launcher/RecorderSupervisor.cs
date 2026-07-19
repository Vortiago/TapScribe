using System.Diagnostics;
using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Launcher;

/// <summary>What the tray shows about the Recorder.</summary>
internal enum RecorderState
{
    Preflight,
    Running,
    Stopped,
    Failed,
}

/// <summary>
/// Runs the Bundle's two child processes: <c>tapscribe.preflight</c> to completion, then
/// the Recorder itself, with both children's stdout/stderr pumped into the Launcher's
/// log.
///
/// Deliberately thin. Every decision it makes — which interpreter, which argv, which
/// environment — comes from <see cref="RecorderCommand"/> in the cross-platform Core and
/// is unit-tested there; what is left here is <c>Process.Start</c>, two event handlers,
/// and a kill. Reaping is not this class's job: the <see cref="JobObject"/> the Launcher
/// holds covers the grandchildren (<c>whisperlivekit-server</c>) that this class never
/// gets a handle to.
///
/// <b>Not unit-tested — needs a real Windows interpreter to spawn.</b>
/// </summary>
internal sealed class RecorderSupervisor : IDisposable
{
    private readonly BundleLayout _layout;
    private readonly JobObject? _job;
    private readonly Action<string> _log;
    private readonly Action<RecorderState, string> _onState;
    private readonly Lock _gate = new();
    private Process? _recorder;
    private bool _stopping;

    public RecorderSupervisor(BundleLayout layout, JobObject? job, Action<string> log, Action<RecorderState, string> onState)
    {
        _layout = layout;
        _job = job;
        _log = log;
        _onState = onState;
    }

    /// <summary>Boot the Recorder on a background thread; the tray keeps pumping messages.</summary>
    public void Start() => Task.Run(Run);

    private void Run()
    {
        string wheel;
        try
        {
            Directory.CreateDirectory(_layout.DataDirectory);
            wheel = _layout.ResolveWheel();
        }
        catch (Exception error) when (error is BundleLayoutException or IOException or UnauthorizedAccessException)
        {
            Fail(error.Message);
            return;
        }

        _log($"program dir: {_layout.ProgramDirectory}");
        _log($"data dir:    {_layout.DataDirectory}");
        _log($"wheel:       {wheel}");

        _onState(RecorderState.Preflight, "Preparing TapScribe…");
        if (!RunPreflight(wheel))
            return;

        StartRecorder(wheel);
    }

    /// <summary>
    /// Blocking, logged. A non-zero exit is reported but does NOT stop the Recorder:
    /// preflight's steps are repairs (the CUDA torch swap, the silero-vad probe), and a
    /// failed repair still leaves a Recorder that boots — with a degraded backend the
    /// operator can see in the log and fix from /setup.
    /// </summary>
    private bool RunPreflight(string wheel)
    {
        BundleProcess command = RecorderCommand.Preflight(_layout, wheel);
        try
        {
            using Process process = Spawn(command);
            process.WaitForExit();
            if (process.ExitCode != 0)
                _log($"preflight exited {process.ExitCode} — continuing; some optional components may be missing.");
            return true;
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            // Could not launch the interpreter at all — a broken install, not a failed
            // repair. There is nothing to start after this.
            Fail($"Could not run {command.Executable}: {error.Message}");
            return false;
        }
    }

    private void StartRecorder(string wheel)
    {
        BundleProcess command = RecorderCommand.Recorder(_layout, wheel);
        try
        {
            Process process = Spawn(command);
            lock (_gate)
                _recorder = process;

            process.EnableRaisingEvents = true;
            process.Exited += (_, _) =>
            {
                lock (_gate)
                {
                    if (_stopping)
                        return;
                }

                _log($"recorder exited with code {process.ExitCode}");
                _onState(
                    RecorderState.Stopped,
                    $"TapScribe stopped unexpectedly (exit {process.ExitCode}). See the log.");
            };

            _onState(RecorderState.Running, "TapScribe is running.");
        }
        catch (Exception error) when (error is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            Fail($"Could not start the Recorder ({command.Executable}): {error.Message}");
        }
    }

    /// <summary>
    /// Ask the Recorder to go away, then let the job object take the rest. The kill is
    /// <c>entireProcessTree</c> for the ordinary case; the job is the backstop for the
    /// case this misses (a grandchild that re-parented).
    /// </summary>
    public void Stop()
    {
        Process? process;
        lock (_gate)
        {
            _stopping = true;
            process = _recorder;
            _recorder = null;
        }

        if (process is null)
            return;

        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                process.WaitForExit(5000);
            }
        }
        catch (Exception error) when (error is InvalidOperationException or System.ComponentModel.Win32Exception or NotSupportedException)
        {
            // Already gone, or exited between HasExited and Kill. Nothing to do — and the
            // job object's KILL_ON_JOB_CLOSE reaps anything still alive when we exit,
            // which is exactly the leak this Stop() is trying to avoid.
            _log($"stop: {error.Message}");
        }
        finally
        {
            process.Dispose();
        }
    }

    /// <summary>Spawn one <see cref="BundleProcess"/> with both streams pumped into the log.</summary>
    private Process Spawn(BundleProcess command)
    {
        var info = new ProcessStartInfo(command.Executable)
        {
            // Argv as a list, never a shell string (CLAUDE.md) — ArgumentList quotes each
            // token for us, so a program dir with a space in it stays one argument.
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = _layout.ProgramDirectory,
        };
        foreach (string argument in command.Arguments)
            info.ArgumentList.Add(argument);
        foreach (KeyValuePair<string, string> variable in command.Environment)
            info.Environment[variable.Key] = variable.Value;

        var process = new Process { StartInfo = info };
        process.OutputDataReceived += (_, e) => { if (e.Data is not null) _log(e.Data); };
        process.ErrorDataReceived += (_, e) => { if (e.Data is not null) _log(e.Data); };

        _log($"$ {command.Executable} {string.Join(' ', command.Arguments)}");
        process.Start();

        // Only when the Launcher could NOT put itself in the job: otherwise the child is
        // already a member by inheritance and a second assignment fails.
        if (_job is { SelfAssigned: false } job && !job.AssignProcess(process.Handle))
            _log("job object: could not assign the child — a crash may leave whisperlivekit-server running.");

        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        return process;
    }

    private void Fail(string message)
    {
        _log($"FATAL: {message}");
        _onState(RecorderState.Failed, message);
    }

    public void Dispose() => Stop();
}
