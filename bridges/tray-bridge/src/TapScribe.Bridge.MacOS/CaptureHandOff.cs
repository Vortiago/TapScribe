using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The hand-off off CoreAudio's realtime IO thread, shared by every capture this backend opens.
///
/// The IOProc must return within one device buffer period, about 10 ms at 48 kHz, or CoreAudio
/// drops it. Everything behind <see cref="IAudioCapture.DataAvailable"/> allocates per buffer and
/// takes locks the tap pump and the mute notification also take, so the IOProc does only what it
/// must: copy out of CoreAudio's buffer, which is recycled the moment it returns, into a
/// pre-allocated slot. A pump thread raises the event.
///
/// The producer allocates nothing. Not lock-free: SemaphoreSlim.Release takes a monitor to wake a
/// parked waiter, about 400 ns against a 10,667 us deadline. Allocation and unbounded work were the
/// hazards worth removing; a short uncontended monitor is not.
///
/// Hand-rolled rather than a bounded Channel, for payload ownership: a slot cannot be reused until
/// the handler for it RETURNS, a hand-back edge Channel has no concept of, and the producer must
/// know the ring is full BEFORE it stamps a slot rather than after.
///
/// One instance per capture, reused across <see cref="Start"/>/<see cref="Stop"/>. Its own class
/// because the mic and the system tap need it identically while their lifecycles differ: one opens
/// a device, the other builds a tap inside an aggregate and rebinds it when the default output
/// moves.
/// </summary>
internal sealed class CaptureHandOff
{
    private const int RingSlots = 8;

    // One device buffer, not one second: CoreAudio's period is 512 frames typically and 4096 at the
    // ceiling, so this is double the largest expected. Sizing by SampleRate instead put every slot
    // over the 85 KB LOH threshold, 3 MB per capture at 48 kHz stereo and 98 MB on a 64-channel
    // aggregate, pinned for the meeting. Pump slack is RingSlots, not slot size.
    private const int MaxBufferFrames = 8192;

    /// <summary>How long <see cref="Stop"/> waits for the pump before abandoning it. Sized like its
    /// siblings (<c>TapSession.DisposeDrainTimeout</c>, <c>CaptureOrchestrator.AbandonTeardownCap</c>):
    /// an ordinary handler finishes inside it and the tray's quit budget still fits around it.
    /// </summary>
    private static readonly TimeSpan PumpStopCap = TimeSpan.FromSeconds(2);

    private readonly string _threadName;
    private readonly Action<ReadOnlyMemory<byte>> _deliver;

    private long _dropped;              // producer-only, diagnostic; reset per Start
    private long _handlerFaults;        // pump-only, diagnostic; spans generations

    // The running generation, or null while stopped. Volatile because the IO thread reads it and
    // Stop clears it; null is what tells a callback arriving after teardown there is nowhere to put
    // its buffer.
    private volatile Ring? _active;
    private Thread? _pump;
    private CancellationTokenSource? _pumping;

    /// <param name="threadName">What the pump thread is called, so a stack from a wedged meeting
    /// names its device.</param>
    /// <param name="deliver">Raises the capture's <see cref="IAudioCapture.DataAvailable"/>, on the
    /// pump thread, one buffer at a time and in order. The buffer stays valid until it returns.
    /// </param>
    internal CaptureHandOff(string threadName, Action<ReadOnlyMemory<byte>> deliver)
    {
        _threadName = threadName;
        _deliver = deliver;
    }

    /// <summary>One Start's worth of hand-off: slots, lengths, counters and the semaphore that
    /// publishes them.
    ///
    /// Per generation rather than in fields, because <see cref="Stop"/>'s join is BOUNDED. A pump
    /// abandoned at the cap advances its read counter once more when its handler returns. In shared
    /// fields that write lands on the next Start's ring and desyncs it: the producer misjudges
    /// fullness and the new pump reads a slot nobody wrote. Per generation, the stray write hits a
    /// ring nobody reads.</summary>
    private sealed class Ring
    {
        internal readonly byte[][] Slots = new byte[RingSlots][];
        internal readonly int[] Lengths = new int[RingSlots];
        internal readonly SemaphoreSlim Filled = new(0);
        internal long Written;          // producer-only; the semaphore carries publication
        internal long Read;             // published with Volatile.Write, read by the producer

        // Every slot up front, so the IO thread never allocates.
        internal Ring(int slotBytes)
        {
            for (int i = 0; i < RingSlots; i++)
                Slots[i] = new byte[slotBytes];
        }
    }

    /// <summary>Buffers CoreAudio delivered that the pump never got to. Non-zero means the machine
    /// could not keep up.</summary>
    internal long DroppedBuffers => Interlocked.Read(ref _dropped);

    /// <summary>How many buffers the handler threw on. An exception escaping the pump thread would
    /// end the PROCESS, so they are contained; this is the trace they leave.</summary>
    internal long HandlerFaults => Interlocked.Read(ref _handlerFaults);

    /// <summary>Whether a pump is running. The generation is the pump's only root, so this also
    /// answers "did a failed Start leave a thread parked on a semaphore nobody will release".
    /// </summary>
    internal bool IsPumping => _active is not null;

    /// <summary>Allocate this generation's slots and start the pump, so the first buffer has
    /// somewhere to go. Called BEFORE the IOProc is registered; every way that registration can
    /// fail owes a <see cref="Stop"/>.</summary>
    /// <param name="format">The device format, which sizes a slot: one buffer period of interleaved
    /// frames.</param>
    internal void Start(AudioFormat format)
    {
        ArgumentNullException.ThrowIfNull(format);
        _dropped = 0;
        var ring = new Ring(MaxBufferFrames * format.BytesPerInterleavedFrame);
        var pumping = new CancellationTokenSource();
        // IsBackground so a pump nobody stopped cannot hold the process up.
        var pump = new Thread(() => PumpLoop(ring, pumping.Token))
        {
            IsBackground = true,
            Name = _threadName,
        };
        _active = ring;
        _pumping = pumping;
        _pump = pump;
        pump.Start();
    }

    /// <summary>Take one buffer off the IO thread. Allocation-free: everything it touches was sized
    /// in <see cref="Start"/>.</summary>
    /// <param name="audio">CoreAudio's own buffer, valid only for this call.</param>
    internal void Write(ReadOnlySpan<byte> audio)
    {
        Ring? ring = _active;
        if (ring is null)
            return;

        // Full ring means the pump is behind. Drop THIS buffer rather than overwrite one it has not
        // read; blocking instead would miss the deadline, the one thing this thread must not do.
        if (ring.Written - Volatile.Read(ref ring.Read) >= RingSlots)
        {
            _dropped++;
            return;
        }

        int slot = (int)(ring.Written % RingSlots);
        byte[] target = ring.Slots[slot];
        // This device asks for more than MaxBufferFrames. Dropping beats allocating here, and every
        // buffer from such a device drops, so the count is how it gets diagnosed.
        if (audio.Length > target.Length)
        {
            _dropped++;
            return;
        }

        audio.CopyTo(target);
        ring.Lengths[slot] = audio.Length;
        // Plain increment: only this thread reads it. The semaphore's release/wait pair publishes
        // the slot's contents; Read is the one counter that crosses threads.
        ring.Written++;
        ring.Filled.Release();
    }

    /// <summary>Cancel the pump and wait for it to leave. A handler already in flight runs to
    /// completion, which the seam's "the buffer is live until the handler returns" requires.
    ///
    /// The join is bounded: a consumer that blocks forever must not take the tray's teardown with
    /// it. Abandoning at the cap is safe, since the thread is a background one holding only this
    /// generation's ring and can raise at most the handler it is inside.
    ///
    /// Called only AFTER the IOProc is down: the producer writes to the ring and releases the
    /// semaphore, so tearing this down first leaves a live IO thread publishing into a pump that has
    /// gone.</summary>
    internal void Stop()
    {
        _pumping?.Cancel();
        // Clearing the generation releases its slots and tells a callback that outlived the IOProc
        // there is nowhere to put its buffer.
        _active = null;
        _pump?.Join(PumpStopCap);
        _pumping?.Dispose();
        _pumping = null;
        _pump = null;
    }

    // Raises DataAvailable off the IO thread, one buffer at a time and in order: a slot returns to
    // the producer only after the handler for it has returned.
    private void PumpLoop(Ring ring, CancellationToken stopping)
    {
        try
        {
            while (true)
            {
                ring.Filled.Wait(stopping);
                int slot = (int)(ring.Read % RingSlots);
                try
                {
                    _deliver(ring.Slots[slot].AsMemory(0, ring.Lengths[slot]));
                }
                catch (Exception)
                {
                    // Contained, not propagated: this is a thread of this class's own making, so an
                    // escaping exception takes the whole tray down, and the pipeline behind
                    // DataAvailable can fail on one Utterance without the meeting being over.
                    // Counted because a handler throwing on EVERY buffer is otherwise
                    // indistinguishable from a device that never fires.
                    Interlocked.Increment(ref _handlerFaults);
                }

                // Advanced whether or not the handler returned normally: the slot is finished with
                // either way, and never returning it reads as a permanently full ring.
                Volatile.Write(ref ring.Read, ring.Read + 1);
            }
        }
        catch (OperationCanceledException)
        {
            // Stop or Dispose cancelled the wait, the only way out of the loop.
        }
    }
}
