using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The hand-off off CoreAudio's realtime IO thread, shared by every capture this backend
/// opens.
///
/// The IOProc runs on an IO thread with a hard deadline: one device buffer period, about
/// 10 ms at 48 kHz, and a thread that overruns it repeatedly gets its IOProc dropped.
/// Everything behind <see cref="IAudioCapture.DataAvailable"/> was written for the Windows
/// shape, where NAudio raises on a managed thread it owns: it allocates several KB per buffer
/// and takes locks that the tap pump and the mute notification also take, so raising inline
/// would hand CoreAudio's deadline to a GC pause and to lock contention with non-realtime
/// threads.
///
/// So the IOProc does the one thing it must (copy out of CoreAudio's buffer, which is
/// recycled the moment it returns) into a pre-allocated slot, publishes it and returns. A
/// pump thread raises the event.
///
/// The producer allocates nothing. It is NOT lock-free: SemaphoreSlim.Release takes the
/// semaphore's monitor to wake a parked waiter, measured at roughly 400 ns against a
/// 10,667 us deadline, and every managed wake primitive pays that. Allocation and unbounded
/// work were the hazards worth removing; a short uncontended monitor is not one.
///
/// Hand-rolled rather than a bounded Channel, and the reason is payload ownership rather
/// than cost. The producer cannot allocate a byte[] per buffer, so slots have to be a ring
/// either way, and a slot cannot be reused until the handler for it RETURNS, which is a
/// hand-back edge a Channel has no concept of. Its DropWrite also reports fullness after
/// the write, where this has to know before it stamps a slot. A Channel would replace the
/// semaphore and leave the ring, the pre-check and the hand-back in place.
///
/// One instance per capture, reused across its <see cref="Start"/>/<see cref="Stop"/> cycles.
/// It is a class of its own rather than fields on the capture because the mic and the system
/// tap need it identically while their LIFECYCLES differ completely: one opens a device, the
/// other builds a process tap inside an aggregate device and rebinds it when the default
/// output moves. Sharing this is what keeps the realtime rules written once.
/// </summary>
internal sealed class CaptureHandOff
{
    private const int RingSlots = 8;

    // A slot holds one device buffer, not one second of audio. CoreAudio's period is a few ms
    // (512 frames is typical, 4096 the usual ceiling), so 8192 frames is double the largest
    // period a device is expected to ask for. Sizing by SampleRate instead put every slot over
    // the 85 KB large-object threshold: 384 KB each and 3 MB per capture at 48 kHz stereo, and
    // 98 MB on a 64-channel aggregate, all of it pinned in the LOH for the meeting. The depth
    // that actually matters is RingSlots (8 buffers of pump slack), which slot size does not
    // affect.
    private const int MaxBufferFrames = 8192;

    /// <summary>How long <see cref="Stop"/> waits for the pump to leave before abandoning it.
    /// Sized like its siblings (<c>TapSession.DisposeDrainTimeout</c>,
    /// <c>CaptureOrchestrator.AbandonTeardownCap</c>): long enough that an ordinary handler
    /// finishes, short enough that the tray's own quit budget still fits around it. Overrunning
    /// it abandons a background thread that can raise at most the handler it is already
    /// inside.</summary>
    private static readonly TimeSpan PumpStopCap = TimeSpan.FromSeconds(2);

    private readonly string _threadName;
    private readonly Action<ReadOnlyMemory<byte>> _deliver;

    private long _dropped;              // producer-only, diagnostic; reset per Start
    private long _handlerFaults;        // pump-only, diagnostic; spans generations

    // The running generation, or null while stopped. Volatile because the IO thread reads it
    // and Stop clears it; null is also what tells a callback arriving after teardown that
    // there is nowhere left to put it.
    private volatile Ring? _active;
    private Thread? _pump;
    private CancellationTokenSource? _pumping;

    /// <summary>Build the hand-off for one capture.</summary>
    /// <param name="threadName">What the pump thread is called, so a stack from a wedged
    /// meeting names the device it belongs to.</param>
    /// <param name="deliver">Raises the capture's <see cref="IAudioCapture.DataAvailable"/>.
    /// Called on the pump thread, one buffer at a time and in order, and the buffer it is
    /// handed stays valid until it returns.</param>
    internal CaptureHandOff(string threadName, Action<ReadOnlyMemory<byte>> deliver)
    {
        _threadName = threadName;
        _deliver = deliver;
    }

    /// <summary>One Start's worth of hand-off: the slots, their lengths, the counters and the
    /// semaphore that publishes them.
    ///
    /// Scoped to the generation rather than held in fields on the hand-off, because
    /// <see cref="Stop"/>'s join is BOUNDED. A pump abandoned at the cap is still inside
    /// its handler, and advances its read counter once more when that handler returns. Held in
    /// shared fields, that write lands on the ring of whatever Start came next and desyncs it:
    /// the producer misjudges fullness and the new pump reads a slot nobody wrote, which is
    /// stale audio delivered out of order. Per generation, the stray write hits a ring nobody
    /// is reading.</summary>
    private sealed class Ring
    {
        internal readonly byte[][] Slots = new byte[RingSlots][];
        internal readonly int[] Lengths = new int[RingSlots];
        internal readonly SemaphoreSlim Filled = new(0);
        internal long Written;          // producer-only; the semaphore carries publication
        internal long Read;             // published with Volatile.Write, read by the producer

        /// <summary>Allocate every slot up front, so the IO thread never does.</summary>
        /// <param name="slotBytes">Bytes one slot holds.</param>
        internal Ring(int slotBytes)
        {
            for (int i = 0; i < RingSlots; i++)
                Slots[i] = new byte[slotBytes];
        }
    }

    /// <summary>Buffers CoreAudio delivered that the pump never got to, because it was still
    /// behind when the ring filled. Non-zero means the machine could not keep up; the count is
    /// what a later slice would surface rather than guess at.</summary>
    internal long DroppedBuffers => Interlocked.Read(ref _dropped);

    /// <summary>How many buffers the handler threw on. The pump runs on a thread of its own,
    /// so an escaping exception ends the PROCESS rather than the buffer it is about; this is
    /// the trace containing it leaves, for the reason <c>CoreAudioHal.CallbackFaults</c>
    /// exists.</summary>
    internal long HandlerFaults => Interlocked.Read(ref _handlerFaults);

    /// <summary>Whether a pump thread is running right now. The generation is the pump's only
    /// root, so this is also the answer to "did a failed Start leave a thread parked on a
    /// semaphore nobody will release", which is otherwise observable only as a collectability
    /// question.</summary>
    internal bool IsPumping => _active is not null;

    /// <summary>Allocate this generation's slots and start the pump, so the first buffer has
    /// somewhere to go. Called BEFORE the IOProc is registered; every way that registration
    /// can fail owes a <see cref="Stop"/>.</summary>
    /// <param name="format">The device format, which is what sizes a slot: one buffer period
    /// of interleaved frames.</param>
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

    /// <summary>Take one buffer off the IO thread. Allocation-free: everything it touches was
    /// sized in <see cref="Start"/>. Its one blocking call is the semaphore release, which is
    /// bounded and measured (see the note on this class).</summary>
    /// <param name="audio">CoreAudio's own buffer, valid only for this call.</param>
    internal void Write(ReadOnlySpan<byte> audio)
    {
        Ring? ring = _active;
        if (ring is null)
            return;

        // Full ring means the pump is behind. Drop THIS buffer rather than overwrite one the
        // pump has not read: overwriting would hand it a slot changing underneath it, and
        // blocking would miss the deadline, which is the one thing this thread must not do.
        if (ring.Written - Volatile.Read(ref ring.Read) >= RingSlots)
        {
            _dropped++;
            return;
        }

        int slot = (int)(ring.Written % RingSlots);
        byte[] target = ring.Slots[slot];
        // Larger than the period Start sized for, which means this device asks for more than
        // MaxBufferFrames. Dropping beats allocating on this thread, and every buffer from
        // such a device drops, so the count is how it would be diagnosed.
        if (audio.Length > target.Length)
        {
            _dropped++;
            return;
        }

        audio.CopyTo(target);
        ring.Lengths[slot] = audio.Length;
        // Plain increment: only this thread reads it. The semaphore's release/wait pair is
        // what publishes the slot's contents to the pump, and Read is the one counter that
        // genuinely crosses threads.
        ring.Written++;
        ring.Filled.Release();
    }

    /// <summary>Cancel the pump and wait for it to leave. Cancelling makes its next Wait throw,
    /// so it raises nothing further; a handler already in flight still runs to completion,
    /// which is what the seam's "the buffer is live until the handler returns" requires.
    ///
    /// The join is bounded rather than indefinite: a consumer that blocks forever must not take
    /// the tray's teardown with it. If the cap expires the thread is abandoned, which is safe
    /// because it is a background thread holding only this generation's ring, and because it
    /// can raise at most the one handler it was already inside.
    ///
    /// Called only AFTER the IOProc is down, never before: the producer writes to the ring and
    /// releases the semaphore, so tearing this down first would leave a live IO thread
    /// publishing into a pump that has gone.</summary>
    internal void Stop()
    {
        _pumping?.Cancel();
        // Clearing the generation is also what releases its slots, and what tells a callback
        // that outlived the IOProc there is nowhere to put its buffer.
        _active = null;
        _pump?.Join(PumpStopCap);
        _pumping?.Dispose();
        _pumping = null;
        _pump = null;
    }

    // The pump. Raises DataAvailable off the IO thread, one buffer at a time and in order, so
    // the seam's "the buffer is reusable once the handler returns" still holds: a slot is only
    // returned to the producer after the handler for it has returned.
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
                    // Contained rather than propagated, because this is a thread of this
                    // class's own making: an exception leaving it takes the whole tray down,
                    // and the pipeline behind DataAvailable can raise on one Utterance (a tap
                    // that will not open) without the meeting being over. The Windows sibling
                    // never faces this - NAudio owns its record thread and reports a throwing
                    // handler as RecordingStopped - so containing it is what makes one buffer
                    // cost the same on both backends. Counted for the reason CoreAudioHal
                    // counts its trampoline faults: a handler that throws on EVERY buffer is
                    // otherwise indistinguishable from a device that never fires.
                    Interlocked.Increment(ref _handlerFaults);
                }

                // Advanced whether or not the handler returned normally: the slot is finished
                // with either way, and never returning it would have the producer read the
                // ring as permanently full and drop every buffer after it.
                Volatile.Write(ref ring.Read, ring.Read + 1);
            }
        }
        catch (OperationCanceledException)
        {
            // Stop or Dispose cancelled the wait, which is the only way out of the loop.
        }
    }
}
