using System.Globalization;
using System.Text;
using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Launcher;

/// <summary>
/// The Launcher's log file: every line the Recorder writes to stdout/stderr, plus the
/// Launcher's own lifecycle notes, timestamped and size-bounded. A Bundle has no
/// terminal, so this file is the only place a startup failure can be read after the fact
/// — "Show log" in the tray menu opens it.
///
/// All the policy lives in <see cref="LogRotation"/> (Core, unit-tested on Linux); this
/// class only performs what the plan says. The file is opened
/// <see cref="FileShare.ReadWrite"/> so the operator can keep it open in Notepad while
/// TapScribe runs.
/// </summary>
internal sealed class RotatingLogWriter : IDisposable
{
    private readonly string _directory;
    private readonly string _fileName;
    private readonly string _path;
    private readonly LogRotationPolicy _policy;
    private readonly Lock _gate = new();
    private StreamWriter? _writer;

    public RotatingLogWriter(BundleLayout layout, LogRotationPolicy? policy = null)
    {
        ArgumentNullException.ThrowIfNull(layout);
        _directory = layout.LogDirectory;
        _fileName = BundleLayout.LogFileName;
        _path = layout.LogFile;
        _policy = policy ?? LogRotationPolicy.Default;
    }

    /// <summary>The file "Show log" opens.</summary>
    public string Path => _path;

    /// <summary>
    /// Append one timestamped line. Never throws: a Launcher that dies because it could
    /// not write its own log is strictly worse than one that runs without a log.
    /// </summary>
    public void Write(string line)
    {
        lock (_gate)
        {
            try
            {
                Rotate();
                StreamWriter writer = _writer ??= Open();
                writer.Write(DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss ", CultureInfo.InvariantCulture));
                writer.WriteLine(line);
                writer.Flush();
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                // The log directory went away, or the disk is full. Dropping the line is
                // the correct behaviour — logging is a convenience here, and a failed
                // write must not take down the tray or stop the Recorder. What is lost is
                // this line (and, once the handle is dropped, until the next successful
                // Open()). Nothing else in the Launcher depends on the log.
                _writer?.Dispose();
                _writer = null;
            }
        }
    }

    /// <summary>Apply the Core's plan: roll if it says so, then delete what it names.</summary>
    private void Rotate()
    {
        Directory.CreateDirectory(_directory);

        LogFileInfo[] present = new DirectoryInfo(_directory)
            .GetFiles()
            .Select(f => new LogFileInfo(f.Name, f.Length))
            .ToArray();

        LogRotationPlan plan = LogRotation.Plan(_fileName, present, DateTimeOffset.Now, _policy);
        if (!plan.ShouldRoll && plan.Delete.Count == 0)
            return;

        if (plan.ShouldRoll && plan.ArchiveName is not null)
        {
            _writer?.Dispose();
            _writer = null;
            File.Move(_path, System.IO.Path.Combine(_directory, plan.ArchiveName));
        }

        foreach (string name in plan.Delete)
        {
            try
            {
                File.Delete(System.IO.Path.Combine(_directory, name));
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                // Someone has the old archive open (Notepad, a virus scanner). Leaving it
                // is fine: the plan is recomputed on every write, so the next pass tries
                // again once the handle is released. Worst case the log dir sits one file
                // over budget for a while.
            }
        }
    }

    private StreamWriter Open() =>
        new(new FileStream(_path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false));

    public void Dispose()
    {
        lock (_gate)
        {
            _writer?.Dispose();
            _writer = null;
        }
    }
}
