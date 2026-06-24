namespace TapScribe.Bridge.Core;

/// <summary>The current tray state, event-driven (no idle polling): set from the
/// Test-connection result, the Start pre-flight, and the per-device connect/fail
/// callbacks the orchestrator already raises.</summary>
public abstract record TrayStatus
{
    /// <summary>Nothing running; no recent connection verdict.</summary>
    public sealed record Idle : TrayStatus;

    /// <summary>A meeting is being started (minting the session, opening devices).</summary>
    public sealed record Starting : TrayStatus;

    /// <summary>A meeting is recording <paramref name="Connected"/> of
    /// <paramref name="Total"/> selected devices.</summary>
    public sealed record Streaming(int Connected, int Total) : TrayStatus;

    /// <summary>A failure the operator must see — a rejected token, an unreachable
    /// Recorder, or a device that dropped.</summary>
    public sealed record Error(string Reason) : TrayStatus;

    /// <summary>The meeting is ending: open taps are draining toward the pipeline
    /// trigger (issue #107).</summary>
    public sealed record Ending : TrayStatus;

    /// <summary>The end-of-meeting pipeline is running; <paramref name="Label"/> is the
    /// live stage line (e.g. "Transcribing 3/12…").</summary>
    public sealed record Processing(string Label) : TrayStatus;

    /// <summary>The pipeline finished and the summary is in hand.</summary>
    public sealed record SummaryReady : TrayStatus;

    /// <summary>The pipeline failed; <paramref name="Reason"/> is the human-readable
    /// stage error.</summary>
    public sealed record PipelineFailed(string Reason) : TrayStatus;
}

/// <summary>Which bundled tray icon to show — the at-a-glance signal.</summary>
public enum TrayIcon
{
    Idle,
    Streaming,
    Error,
}

/// <summary>
/// The view-model the NotifyIcon applies: a context-menu <see cref="Header"/> line, an
/// <see cref="Icon"/> key, and a hover <see cref="Tooltip"/>. Built purely from a
/// <see cref="TrayStatus"/> by <see cref="For"/>, so the status presentation is
/// unit-tested without WinForms.
/// </summary>
public sealed record StatusView(string Header, TrayIcon Icon, string Tooltip)
{
    public static StatusView For(TrayStatus status)
    {
        ArgumentNullException.ThrowIfNull(status);
        return status switch
        {
            TrayStatus.Streaming s => new StatusView(
                $"● Streaming — {s.Connected}/{s.Total} devices",
                TrayIcon.Streaming,
                $"TapScribe — recording {s.Connected} of {s.Total} device(s)"),
            TrayStatus.Error e => new StatusView(
                $"⚠ {e.Reason}",
                TrayIcon.Error,
                $"TapScribe — {e.Reason}"),
            TrayStatus.Starting => new StatusView(
                "○ Starting…",
                TrayIcon.Idle,
                "TapScribe — starting…"),
            TrayStatus.Ending => new StatusView(
                "● Ending meeting…",
                TrayIcon.Streaming,
                "TapScribe — ending meeting…"),
            TrayStatus.Processing p => new StatusView(
                $"● {p.Label}",
                TrayIcon.Streaming,
                $"TapScribe — {p.Label}"),
            TrayStatus.SummaryReady => new StatusView(
                "○ Summary ready",
                TrayIcon.Idle,
                "TapScribe — summary ready"),
            TrayStatus.PipelineFailed f => new StatusView(
                $"⚠ {f.Reason}",
                TrayIcon.Error,
                $"TapScribe — {f.Reason}"),
            _ => new StatusView(
                "○ Idle",
                TrayIcon.Idle,
                "TapScribe — idle"),
        };
    }
}
