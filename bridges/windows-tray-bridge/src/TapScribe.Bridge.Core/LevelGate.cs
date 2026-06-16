using System.Buffers.Binary;

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
/// N small pushes matches one big push. Not thread-safe: drive it from one thread
/// (the resampler output of one capture pipeline).
/// </summary>
public sealed class LevelGate
{
    private const double FrameMs = 1000.0 * TapWire.FrameSamples / TapWire.SampleRate; // 20 ms

    private readonly double _openThreshold;
    private readonly int _preRollFrames;
    private readonly int _hangoverFrames;

    private readonly FrameChunker _chunker = new();
    // Recent below-threshold frames while closed (capacity _preRollFrames), so an
    // open can replay the audio that came just before the threshold crossing.
    private readonly Queue<byte[]> _preRoll = new();

    private bool _open;
    private int _silenceFrames; // consecutive below-threshold frames while open

    public LevelGate(GateOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (options.OpenThreshold is < 0 or >= 1 || double.IsNaN(options.OpenThreshold))
            throw new ArgumentOutOfRangeException(nameof(options), "OpenThreshold must be in [0, 1).");
        if (options.Hangover < TimeSpan.Zero)
            throw new ArgumentOutOfRangeException(nameof(options), "Hangover must be non-negative.");
        if (options.PreRoll < TimeSpan.Zero)
            throw new ArgumentOutOfRangeException(nameof(options), "PreRoll must be non-negative.");

        _openThreshold = options.OpenThreshold;
        _preRollFrames = FramesFor(options.PreRoll);
        // At least one silent frame must elapse to close, otherwise a 0 ms
        // hangover would close every utterance on its first frame.
        _hangoverFrames = Math.Max(1, FramesFor(options.Hangover));
    }

    /// <summary>True while an utterance is open (between an Opened and its Closed).</summary>
    public bool IsOpen => _open;

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
        bool active = Rms(frame) >= _openThreshold;

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
                // oldest beyond the window. With _preRollFrames == 0 this discards
                // it immediately (pre-roll disabled).
                _preRoll.Enqueue(frame);
                while (_preRoll.Count > _preRollFrames)
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
        else if (++_silenceFrames >= _hangoverFrames)
        {
            _open = false;
            _silenceFrames = 0;
            events.Add(GateEvent.Closed);
        }
    }

    /// <summary>
    /// RMS of one int16 frame, normalised to [0, 1] (each sample divided by
    /// 32768). RMS rather than peak so a single click can't open the gate.
    /// </summary>
    private static double Rms(ReadOnlySpan<byte> frame)
    {
        int samples = frame.Length / 2;
        if (samples == 0)
            return 0;

        double sumSquares = 0;
        for (int i = 0; i < samples; i++)
        {
            double v = BinaryPrimitives.ReadInt16LittleEndian(frame.Slice(i * 2, 2)) / 32768.0;
            sumSquares += v * v;
        }
        return Math.Sqrt(sumSquares / samples);
    }
}
