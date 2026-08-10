namespace TapScribe.Bridge.Core;

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

    public DeviceTally(int total)
    {
        ArgumentOutOfRangeException.ThrowIfNegative(total);
        Total = total;
    }

    /// <summary>How many devices the meeting was started on.</summary>
    public int Total { get; }

    /// <summary>A device's tap connected: it is streaming. Returns the status to apply.</summary>
    public TrayStatus Connected(string identity)
    {
        ArgumentNullException.ThrowIfNull(identity);
        _live.Add(identity);
        // A tap landing clears this device's earlier drop: a first-connect failure is
        // terminal for that Utterance only, so the next one recovering is the signal that
        // the device is streaming again. Without this the warning would outlive the fault
        // for the rest of the meeting.
        _dropped.Remove(identity);
        return Status;
    }

    /// <summary>A device stopped streaming — its tap couldn't reach the Recorder, or the
    /// endpoint was invalidated mid-meeting (unplugged / disabled / default-device
    /// switch). Returns the status to apply.</summary>
    public TrayStatus Dropped(string identity)
    {
        ArgumentNullException.ThrowIfNull(identity);
        _live.Remove(identity);
        _dropped.Add(identity);
        return Status;
    }

    /// <summary>The status the tally currently implies: an <see cref="TrayStatus.Error"/>
    /// naming the devices that stopped (and what is still being recorded) while any is
    /// down, otherwise the plain streaming count. Sorted, so the message is a function of
    /// the state and not of the order the failures happened to arrive in.</summary>
    public TrayStatus Status => _dropped.Count == 0
        ? new TrayStatus.Streaming(_live.Count, Total)
        : new TrayStatus.Error($"{string.Join(", ", _dropped)} stopped — recording {_live.Count}/{Total} devices");
}
