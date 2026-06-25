namespace TapScribe.Bridge.Core;

/// <summary>The kind of boundary a <see cref="LevelGate"/> emits.</summary>
public enum GateEventKind
{
    /// <summary>The first frame of a new utterance (the oldest pre-roll frame, or
    /// the triggering frame when there is no pre-roll). The consumer starts a tap
    /// here.</summary>
    Opened,

    /// <summary>A subsequent 640-byte frame of the open utterance (pre-roll tail,
    /// speech, or trailing hangover silence).</summary>
    Audio,

    /// <summary>The open utterance just ended (silence held for the hangover).
    /// <see cref="GateEvent.Frame"/> is empty.</summary>
    Closed,
}

/// <summary>One boundary or 640-byte frame emitted by <see cref="LevelGate.Push"/>.</summary>
public readonly record struct GateEvent(GateEventKind Kind, byte[] Frame)
{
    public static GateEvent Opened(byte[] frame) => new(GateEventKind.Opened, frame);
    public static GateEvent Audio(byte[] frame) => new(GateEventKind.Audio, frame);
    public static readonly GateEvent Closed = new(GateEventKind.Closed, []);
}

/// <summary>
/// The Bridge-side Mute: turns a continuous 16 kHz mono int16 PCM stream into
/// gated Utterances, one 640-byte frame at a time. While closed it watches the
/// per-frame level and keeps a short pre-roll ring; when a 20 ms frame's RMS
/// crosses <see cref="GateOptions.OpenThreshold"/> it opens an utterance —
/// replaying the buffered pre-roll frames so leading audio isn't clipped — and
/// streams every following frame as <see cref="GateEventKind.Audio"/> until the
/// level stays below the threshold for <see cref="GateOptions.Hangover"/>, then
/// closes.
///
/// Framing is delegated to <see cref="FrameChunker"/> (the one place that splits a
/// byte stream into exact 640-byte frames), so the gate's output is already
/// frame-aligned and the consumer streams each <see cref="GateEvent.Frame"/>
/// straight to a tap with no further chunking. Pure and synchronous: the output of
/// N small pushes matches one big push. Drive <see cref="Push"/> from one thread
/// (the resampler output of one capture pipeline); the one exception is
/// <see cref="UpdateTuning"/>, which is safe to call concurrently from another
/// thread (e.g. the tray's Settings → Save) to re-tune a running gate.
/// </summary>
public sealed class LevelGate
{
    private const double FrameMs = 1000.0 * TapWire.FrameSamples / TapWire.SampleRate; // 20 ms

    /// <summary>The three knobs as one immutable snapshot. Held behind a single
    /// reference so <see cref="UpdateTuning"/> can swap the whole set atomically: a
    /// reference assignment is atomic, whereas updating the 64-bit
    /// <see cref="OpenThreshold"/> field on its own could tear under the capture
    /// thread reading it in <see cref="Push"/>.</summary>
    private sealed record Tuning(double OpenThreshold, int PreRollFrames, int HangoverFrames);

    private Tuning _tuning;

    private readonly FrameChunker _chunker = new();
    // Recent below-threshold frames while closed (capacity Tuning.PreRollFrames), so an
    // open can replay the audio that came just before the threshold crossing.
    private readonly Queue<byte[]> _preRoll = new();

    private bool _open;
    private int _silenceFrames; // consecutive below-threshold frames while open

    public LevelGate(GateOptions options) => _tuning = BuildTuning(options);

    /// <summary>True while an utterance is open (between an Opened and its Closed).</summary>
    public bool IsOpen => _open;

    /// <summary>
    /// Force the gate back to its just-constructed state — closed, no accrued silence,
    /// empty pre-roll, no partial frame — without emitting a <see cref="GateEventKind.Closed"/>.
    /// The pipeline calls this when capture is interrupted out-of-band (the device mutes,
    /// #159): without it an utterance that was open when the interruption hit would leave
    /// the gate <see cref="IsOpen"/>, so the first resumed frame would be streamed as a
    /// continuation (<see cref="GateEventKind.Audio"/>) into a tap that no longer exists
    /// instead of opening a fresh one. Drive it from the same thread as <see cref="Push"/>;
    /// the tuning (which a concurrent <see cref="UpdateTuning"/> may swap) is left intact.
    /// </summary>
    public void Reset()
    {
        _open = false;
        _silenceFrames = 0;
        _preRoll.Clear();
        _chunker.Reset();
    }

    /// <summary>
    /// Re-tune the gate at runtime (sensitivity / hangover / pre-roll) without tearing
    /// it down. Safe to call from a different thread while the capture thread drives
    /// <see cref="Push"/>: the new tuning is validated, then published as one atomic
    /// reference swap, so a push never sees a torn threshold. An in-flight open
    /// utterance is not torn down — its open-state and running silence count live
    /// outside the snapshot and are preserved — so the new tuning governs every frame
    /// from the next push onward, including the already-accrued silence (shortening the
    /// hangover mid-utterance can therefore close it on the next silent frame rather
    /// than only counting silence that starts after the change). Validation matches the
    /// constructor; an out-of-range value throws and leaves the current tuning in place.
    /// </summary>
    public void UpdateTuning(GateOptions options) => Volatile.Write(ref _tuning, BuildTuning(options));

    private static Tuning BuildTuning(GateOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (options.OpenThreshold is < 0 or >= 1 || double.IsNaN(options.OpenThreshold))
            throw new ArgumentOutOfRangeException(nameof(options), "OpenThreshold must be in [0, 1).");
        if (options.Hangover < TimeSpan.Zero)
            throw new ArgumentOutOfRangeException(nameof(options), "Hangover must be non-negative.");
        if (options.PreRoll < TimeSpan.Zero)
            throw new ArgumentOutOfRangeException(nameof(options), "PreRoll must be non-negative.");

        // At least one silent frame must elapse to close, otherwise a 0 ms hangover
        // would close every utterance on its first frame.
        return new Tuning(options.OpenThreshold, FramesFor(options.PreRoll), Math.Max(1, FramesFor(options.Hangover)));
    }

    private static int FramesFor(TimeSpan span) => (int)Math.Round(span.TotalMilliseconds / FrameMs);

    /// <summary>
    /// Feed the next chunk of 16 kHz mono int16 PCM and return the gate events it
    /// produced — zero or more, in order, each carrying one 640-byte frame. The
    /// trailing sub-frame bytes are retained for the next call.
    /// </summary>
    public IReadOnlyList<GateEvent> Push(ReadOnlySpan<byte> pcm)
    {
        List<GateEvent>? events = null;
        foreach (byte[] frame in _chunker.Push(pcm))
            ProcessFrame(frame, events ??= []);
        return events ?? (IReadOnlyList<GateEvent>)Array.Empty<GateEvent>();
    }

    private void ProcessFrame(byte[] frame, List<GateEvent> events)
    {
        // One torn-free snapshot per frame, so a concurrent UpdateTuning either lands
        // fully before this frame or fully after it — never half-applied within it.
        Tuning tuning = Volatile.Read(ref _tuning);
        bool active = AudioLevel.Rms(frame) >= tuning.OpenThreshold;

        if (!_open)
        {
            if (active)
            {
                // Open: replay the buffered pre-roll (oldest first) then this
                // triggering frame. The very first emitted frame is the Opened
                // boundary; the rest are Audio.
                _open = true;
                _silenceFrames = 0;
                bool first = true;
                while (_preRoll.TryDequeue(out byte[]? pre))
                {
                    events.Add(first ? GateEvent.Opened(pre) : GateEvent.Audio(pre));
                    first = false;
                }
                events.Add(first ? GateEvent.Opened(frame) : GateEvent.Audio(frame));
            }
            else
            {
                // Still closed: remember this frame as pre-roll, dropping the
                // oldest beyond the window. With PreRollFrames == 0 this discards
                // it immediately (pre-roll disabled).
                _preRoll.Enqueue(frame);
                while (_preRoll.Count > tuning.PreRollFrames)
                    _preRoll.Dequeue();
            }
            return;
        }

        // Open: every frame is part of the utterance, including the trailing
        // hangover silence (the Recorder's strip-silence trims it later).
        events.Add(GateEvent.Audio(frame));
        if (active)
        {
            _silenceFrames = 0;
        }
        else if (++_silenceFrames >= tuning.HangoverFrames)
        {
            _open = false;
            _silenceFrames = 0;
            events.Add(GateEvent.Closed);
        }
    }
}
