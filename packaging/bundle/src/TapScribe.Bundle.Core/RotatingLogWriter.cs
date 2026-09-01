using System.Globalization;
using System.Text;

namespace TapScribe.Bundle.Core;

/// <summary>
/// The tray's log file: every line the Recorder writes to stdout/stderr, plus the
/// tray's own lifecycle notes, timestamped and size-bounded. A Bundle has no
/// terminal, so this file is the only place a startup failure can be read after the fact
/// — "Show log" in the tray menu opens it.
///
/// All the policy lives in <see cref="LogRotation"/> (Core, unit-tested on Linux); this
/// class only performs what the plan says. The file is opened
/// <see cref="FileShare.ReadWrite"/> so the operator can keep it open in Notepad while
/// TapScribe runs.
/// </summary>
public sealed class RotatingLogWriter : IDisposable
{
    private readonly string _directory;
    private readonly string _fileName;
    private readonly string _path;
    private readonly LogRotationPolicy _policy;
    private readonly Lock _gate = new();
    private StreamWriter? _writer;

    /// <summary>
    /// Bytes in the ACTIVE file, tracked so the common write doesn't have to ask the
    /// filesystem. Set from the real length when the file is opened, then advanced per
    /// line; -1 means "unknown, measure on next write".
    /// </summary>
    private long _written = -1;

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
                // Only ask the filesystem when the running byte count says we might have
                // crossed the threshold. The pumps feed this ONE LINE PER EVENT from the
                // Recorder's stdout and stderr (PYTHONUNBUFFERED=1), and preflight pipes
                // pip's torch install through it — thousands of lines. Enumerating the
                // directory and stat-ing every file on each of those, inside the lock
                // both pump threads contend on, is pure waste.
                if (_written < 0 || _written >= _policy.MaxBytes)
                    Rotate();

                StreamWriter writer = _writer ??= Open();
                string stamp = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss ", CultureInfo.InvariantCulture);
                writer.Write(stamp);
                writer.WriteLine(line);
                writer.Flush();
                // The EXACT byte count, taken from the stream after the flush — not a
                // character count. The file is UTF-8 and the Recorder deliberately emits
                // it (PYTHONUTF8), so a log full of non-ASCII speaker names or this
                // repo's em-dashes has meaningfully more bytes than characters. Counting
                // chars would under-measure and let the file grow well past MaxBytes
                // before a roll was even considered. Position is an in-memory field on
                // FileStream once flushed, so this costs nothing.
                _written = writer.BaseStream.Position;
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
                _written = -1;
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
            _written = -1;
            File.Move(_path, System.IO.Path.Join(_directory, plan.ArchiveName));
        }

        foreach (string name in plan.Delete)
        {
            try
            {
                File.Delete(System.IO.Path.Join(_directory, name));
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

    private StreamWriter Open()
    {
        var stream = new FileStream(_path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
        // Seed the running count from the real file, so an append to an existing log
        // still rolls at the right size instead of starting from zero every launch.
        _written = stream.Length;
        return new StreamWriter(stream, new UTF8Encoding(false));
    }

    public void Dispose()
    {
        lock (_gate)
        {
            _writer?.Dispose();
            _writer = null;
        }
    }
}
