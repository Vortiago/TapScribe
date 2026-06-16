using System.Buffers.Binary;

namespace TapScribe.Bridge.Core;

/// <summary>The kind of boundary a <see cref="LevelGate"/> emits.</summary>
public enum GateEventKind
{
    /// <summary>An utterance just started. <see cref="GateEvent.Pcm"/> carries the
    /// pre-roll plus the first above-threshold frame.</summary>
    Opened,

    /// <summary>One 640-byte frame of an open utterance.</summary>
    Audio,

    /// <summary>The open utterance just ended (silence held for the hangover).
    /// <see cref="GateEvent.Pcm"/> is empty.</summary>
    Closed,
}

/// <summary>One boundary or frame emitted by <see cref="LevelGate.Push"/>.</summary>
public readonly record struct GateEvent(GateEventKind Kind, byte[] Pcm)
{
    public static GateEvent Opened(byte[] pcm) => new(GateEventKind.Opened, pcm);
    public static GateEvent Audio(byte[] pcm) => new(GateEventKind.Audio, pcm);
    public static readonly GateEvent Closed = new(GateEventKind.Closed, []);
}

/// <summary>
/// The Bridge-side Mute: turns a continuous 16 kHz mono int16 PCM stream into
/// gated Utterances. While closed it watches the per-frame level and keeps a
/// short pre-roll ring; when a 20 ms frame's RMS crosses
/// <see cref="GateOptions.OpenThreshold"/> it opens an utterance — emitting the
/// buffered pre-roll plus the triggering frame so leading audio isn't clipped —
/// and streams every following frame as <see cref="GateEventKind.Audio"/> until
/// the level stays below the threshold for <see cref="GateOptions.Hangover"/>,
/// at which point it closes.
///
/// Pure and synchronous: <see cref="Push"/> returns the events for the bytes it
/// was given and holds only a sub-frame remainder and the pre-roll ring between
/// calls, so the whole thing is unit-testable with synthetic PCM — the output of
/// N small pushes matches one big push. Not thread-safe: drive it from one thread
/// (the resampler output of one capture pipeline).
/// </summary>
public sealed class LevelGate
{
    private const double FrameMs = 1000.0 * TapWire.FrameSamples / TapWire.SampleRate; // 20 ms

    private readonly double _openThreshold;
    private readonly int _preRollFrames;
    private readonly int _hangoverFrames;

    // Recent below-threshold frames while closed (capacity _preRollFrames), so an
    // open can prepend the audio that came just before the threshold crossing.
    private readonly Queue<byte[]> _preRoll = new();
    // Carried sub-frame bytes so frame decisions stay 640-byte aligned even when a
    // backend hands us an odd-sized resampled chunk.
    private byte[] _pending = [];

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
    /// produced — zero or more, in order. The trailing sub-frame bytes (&lt; 640)
    /// are retained for the next call.
    /// </summary>
    public IReadOnlyList<GateEvent> Push(ReadOnlySpan<byte> pcm)
    {
        var events = new List<GateEvent>();

        byte[] combined;
        if (_pending.Length == 0)
        {
            combined = pcm.ToArray();
        }
        else
        {
            combined = new byte[_pending.Length + pcm.Length];
            _pending.CopyTo(combined, 0);
            pcm.CopyTo(combined.AsSpan(_pending.Length));
        }

        int frameCount = combined.Length / TapWire.FrameBytes;
        for (int i = 0; i < frameCount; i++)
            ProcessFrame(combined[(i * TapWire.FrameBytes)..((i + 1) * TapWire.FrameBytes)], events);

        _pending = combined[(frameCount * TapWire.FrameBytes)..];
        return events;
    }

    private void ProcessFrame(byte[] frame, List<GateEvent> events)
    {
        bool active = Rms(frame) >= _openThreshold;

        if (!_open)
        {
            if (active)
            {
                // Open: the utterance starts with the buffered pre-roll (oldest
                // first) followed by this triggering frame.
                byte[] opening = Concat(_preRoll, frame);
                _preRoll.Clear();
                _open = true;
                _silenceFrames = 0;
                events.Add(GateEvent.Opened(opening));
            }
            else
            {
                // Still closed: remember this frame as pre-roll, dropping the
                // oldest beyond the configured window. With _preRollFrames == 0
                // this discards it immediately (pre-roll disabled).
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

    private static byte[] Concat(Queue<byte[]> preRoll, byte[] last)
    {
        int total = last.Length;
        foreach (byte[] f in preRoll)
            total += f.Length;

        var result = new byte[total];
        int offset = 0;
        foreach (byte[] f in preRoll)
        {
            f.CopyTo(result, offset);
            offset += f.Length;
        }
        last.CopyTo(result, offset);
        return result;
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
