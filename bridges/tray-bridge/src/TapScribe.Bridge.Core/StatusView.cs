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

    /// <summary>Streaming, but short a device. Its own key rather than reusing
    /// <see cref="Error"/>, since the meeting IS running and must not read as stopped, and rather
    /// than <see cref="Streaming"/>, which is what made a half-recorded meeting invisible.</summary>
    Degraded,
    Error,
}

/// <summary>
/// The view-model a tray applies: a menu <see cref="Header"/> line, an <see cref="Icon"/> key,
/// a hover <see cref="Tooltip"/>, and a <see cref="Badge"/> for the shell to put BESIDE the
/// glyph. Built purely from a <see cref="TrayStatus"/> by <see cref="For"/>, so the status
/// presentation is unit-tested without WinForms or AppKit.
/// </summary>
/// <param name="Badge">A few characters beside the menu-bar glyph, or "" for nothing to say. The
/// header is behind a click and the glyph is easy to miss, so this is the only part of a status an
/// operator sees while they are on the call. An exception report, not a readout.</param>
public sealed record StatusView(string Header, TrayIcon Icon, string Tooltip, string Badge = "")
{
    public static StatusView For(TrayStatus status)
    {
        ArgumentNullException.ThrowIfNull(status);
        return status switch
        {
            // Short a device: the meeting is running and is not recording what the operator asked
            // for, so both the glyph and the badge change and shape and number each carry it.
            TrayStatus.Streaming s => new StatusView(
                $"● Streaming — {s.Connected}/{s.Total} devices",
                s.Connected < s.Total ? TrayIcon.Degraded : TrayIcon.Streaming,
                $"TapScribe — recording {s.Connected} of {s.Total} device(s)",
                s.Connected < s.Total ? $"{s.Connected}/{s.Total}" : ""),
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
