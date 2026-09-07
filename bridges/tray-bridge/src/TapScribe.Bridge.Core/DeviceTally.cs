namespace TapScribe.Bridge.Core;

/// <summary>
/// What a <see cref="DeviceTally"/> makes of one report about one device: the
/// <paramref name="Status"/> the meeting is now in, and whether the report was a
/// <paramref name="Transition"/> — news the operator has not been told — rather than a repeat
/// of something the tally already knew.
///
/// Both callbacks fire once per Utterance, so the distinction is load-bearing: a device that
/// dropped once goes on reporting it for the rest of the meeting, and a shell that treats
/// every report as news pops a toast every time. It is deliberately NOT the same question as
/// "does this differ from what is on screen" — that one belongs to whoever owns the screen,
/// and a tally has no way to know it.
/// </summary>
public sealed record TallyReport(TrayStatus Status, bool Transition);

/// <summary>
/// Which of a running meeting's devices are actually streaming, and the
/// <see cref="TrayStatus"/> that follows from it — the state behind the status line's
/// "N/M devices", fed by the per-identity connect/fail callbacks
/// <see cref="CaptureOrchestrator.StartAll"/> already raises.
///
/// Pure and shell-free, so the rule lives next to the <see cref="StatusView"/> that
/// renders it rather than as bookkeeping inside a menu-click handler. Not thread-safe:
/// a shell mutates it from ONE thread (the tray marshals both callbacks onto its UI
/// thread before touching it).
/// </summary>
public sealed class DeviceTally
{
    private readonly HashSet<string> _live = new(StringComparer.Ordinal);
    private readonly SortedSet<string> _dropped = new(StringComparer.Ordinal);

    private readonly bool _attached;

    /// <param name="total">How many devices the taps were opened on.</param>
    /// <param name="attached">Whether these taps are an attached tap rather than a bracketed
    /// meeting, which is the ONE thing that differs: the counting is identical and only the
    /// sentence the operator reads changes. Carried here rather than swapped at the render,
    /// because the tally is what the status line is built from and a second place to decide it
    /// is a second place to get it wrong.</param>
    public DeviceTally(int total, bool attached = false)
    {
        ArgumentOutOfRangeException.ThrowIfNegative(total);
        Total = total;
        _attached = attached;
    }

    /// <summary>How many devices the taps were opened on.</summary>
    public int Total { get; }

    /// <summary>A device's tap connected: it is streaming.</summary>
    public TallyReport Connected(string identity)
    {
        ArgumentNullException.ThrowIfNull(identity);
        bool transition = _live.Add(identity);
        // A tap landing clears this device's earlier drop: a first-connect failure is
        // terminal for that Utterance only, so the next one recovering is the signal that
        // the device is streaming again. Without this the warning would outlive the fault
        // for the rest of the meeting.
        transition |= _dropped.Remove(identity);
        return new TallyReport(Status, transition);
    }

    /// <summary>A device stopped streaming — its tap couldn't reach the Recorder, or the
    /// endpoint was invalidated mid-meeting (unplugged / disabled / default-device
    /// switch).</summary>
    public TallyReport Dropped(string identity)
    {
        ArgumentNullException.ThrowIfNull(identity);
        bool transition = _live.Remove(identity);
        transition |= _dropped.Add(identity);
        return new TallyReport(Status, transition);
    }

    /// <summary>The status the tally currently implies: an <see cref="TrayStatus.Error"/>
    /// naming the devices that stopped (and what is still being recorded) while any is
    /// down, otherwise the plain streaming count. Sorted, so the message is a function of
    /// the state and not of the order the failures happened to arrive in.</summary>
    public TrayStatus Status => _dropped.Count == 0
        ? _attached
            ? new TrayStatus.Attached(_live.Count, Total)
            : new TrayStatus.Streaming(_live.Count, Total)
        // The verb follows the mode here too, not just in the healthy sentence: an attached
        // tap is not recording a meeting of its own, it feeds whatever session the Recorder
        // has open, and an operator told a failing one is "recording" would look for an End
        // that is not offered.
        : new TrayStatus.Error(
            $"{string.Join(", ", _dropped)} stopped — "
            + (_attached
                ? $"feeding the current session from {_live.Count}/{Total} devices"
                : $"recording {_live.Count}/{Total} devices"));
}
