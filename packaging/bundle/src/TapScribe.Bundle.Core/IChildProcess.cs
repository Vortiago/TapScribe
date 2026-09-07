using System.Diagnostics;

namespace TapScribe.Bundle.Core;

/// <summary>
/// One spawned child, as <see cref="RecorderSupervisor"/> uses it — and nothing else.
///
/// It exists so the supervisor's DECISIONS are testable on the Linux CI leg: which child
/// to start, when a quit raced a spawn, and above all whether a Recorder that exited
/// immediately means "someone else holds port 8001" or "this install is broken". Those are
/// the parts an operator meets when something is wrong, and they used to be reachable only
/// by installing a Bundle on Windows and breaking it by hand.
///
/// The spawn itself stays untested, deliberately: <see cref="ChildProcess"/> is a handful
/// of forwarding members over <see cref="Process"/>, and a fake of it that also faked
/// process start would be testing the double.
/// </summary>
public interface IChildProcess : IDisposable
{
    bool HasExited { get; }

    int ExitCode { get; }

    /// <summary>The child's native handle, which is what <see cref="IProcessReaper.Adopt"/>
    /// enrols by. A POSIX reaper would want the pid instead; it gets a member when there is
    /// one, rather than every double implementing one nothing calls.</summary>
    IntPtr NativeHandle { get; }

    /// <summary>
    /// The child's process id — what a POSIX reaper needs, where the Windows one needs
    /// <see cref="NativeHandle"/>.
    ///
    /// Its own member rather than a cast of the handle. macOS' reaper used to do
    /// <c>(int)child.NativeHandle</c>, which happens to work on .NET-for-Unix and is a
    /// reinterpretation of a WINDOWS concept: nothing documents the two as the same integer,
    /// and the failure it buys is silent — <c>setpgid</c> on a garbage id returns -1, the
    /// reaper answers false, and the WhisperLiveKit orphan it exists to prevent is back with
    /// nothing in the log worth reading.
    /// </summary>
    int ProcessId { get; }

    /// <summary>Raised when the child exits on its own. Subscribed BEFORE the child is
    /// allowed to raise it: a Recorder that dies instantly (a broken wheel, a port already
    /// held) would otherwise raise into an empty delegate and leave the tray reporting a
    /// Recorder that is not there.</summary>
    event EventHandler? Exited;

    /// <summary>Kill the child and everything under it. Throw-free is NOT promised — the
    /// child may have exited between a check and this call — and the supervisor catches
    /// the documented failures around it.</summary>
    void Kill();

    /// <summary>Block until the child exits, or until <paramref name="milliseconds"/>
    /// elapse. Returns whether it exited.</summary>
    bool WaitForExit(int milliseconds);

    /// <summary>Block until the child exits, however long that takes: preflight's pip
    /// install is unbounded by nature.</summary>
    void WaitForExit();
}

/// <summary>The real one: <see cref="Process"/>, with both streams pumped into the tray's
/// log. Every line of it is forwarding, which is why the seam above exists.</summary>
public sealed class ChildProcess : IChildProcess
{
    private readonly Process _process;

    private ChildProcess(Process process) => _process = process;

    public bool HasExited => _process.HasExited;

    public int ExitCode => _process.ExitCode;

    public event EventHandler? Exited
    {
        add
        {
            _process.Exited += value;
            // AFTER the subscription, never before: setting this registers the wait
            // immediately and, for a process that has ALREADY exited, raises Exited
            // synchronously — and Process latches it so it never fires again.
            _process.EnableRaisingEvents = true;
        }
        remove => _process.Exited -= value;
    }

    public IntPtr NativeHandle => _process.Handle;

    public int ProcessId => _process.Id;

    public void Kill() => _process.Kill(entireProcessTree: true);

    public bool WaitForExit(int milliseconds) => _process.WaitForExit(milliseconds);

    public void WaitForExit() => _process.WaitForExit();

    public void Dispose() => _process.Dispose();

    /// <summary>Start one <see cref="BundleProcess"/> with both streams pumped into
    /// <paramref name="log"/>.</summary>
    public static ChildProcess Start(BundleProcess command, string workingDirectory, Action<string> log)
    {
        ArgumentNullException.ThrowIfNull(command);
        ArgumentException.ThrowIfNullOrWhiteSpace(workingDirectory);
        ArgumentNullException.ThrowIfNull(log);

        var info = new ProcessStartInfo(command.Executable)
        {
            // Argv as a list, never a shell string (CLAUDE.md) — ArgumentList quotes each
            // token for us, so a program dir with a space in it stays one argument.
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            // The child is forced to UTF-8 (RecorderCommand.EnvironmentFor); decode the
            // pipe the same way. Without this .NET uses the console/ANSI code page and the
            // em-dashes this repo's messages are full of arrive mangled in the operator's
            // only diagnostic surface.
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
            WorkingDirectory = workingDirectory,
        };
        foreach (string argument in command.Arguments)
            info.ArgumentList.Add(argument);
        foreach (KeyValuePair<string, string> variable in command.Environment)
            info.Environment[variable.Key] = variable.Value;

        var process = new Process { StartInfo = info };
        process.OutputDataReceived += (_, e) =>
        {
            if (e.Data is not null)
                log(e.Data);
        };
        process.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is not null)
                log(e.Data);
        };

        log($"$ {command.Executable} {string.Join(' ', command.Arguments)}");
        // Ownership transfers to the ChildProcess on the last line and not before: a
        // Win32Exception out of Start (a broken install — the case the supervisor's own
        // catch is written for) would otherwise strand this Process, and with it the pipe
        // handles the two redirections already asked for.
        try
        {
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
        }
        catch
        {
            process.Dispose();
            throw;
        }
        return new ChildProcess(process);
    }
}
