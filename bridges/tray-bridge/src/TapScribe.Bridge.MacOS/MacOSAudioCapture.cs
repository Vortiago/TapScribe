using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS <see cref="IAudioCapture"/>: one CoreAudio input device, streamed through an
/// IOProc. Every native call goes through <see cref="ICoreAudioHal"/>, so this class holds
/// the decisions and none of the plumbing, and the whole of it is exercised on a lane with
/// no audio hardware.
/// </summary>
internal sealed class MacOSAudioCapture : IAudioCapture
{
    private readonly ICoreAudioHal _hal;
    private readonly uint _deviceId;

    // The mute listener, or null when this endpoint carries no mute property. Null is also
    // what says there is nothing to unsubscribe at teardown.
    private readonly IDisposable? _muteListener;

    // Cached so a read from the IO thread never re-enters CoreAudio; refreshed from the
    // property notification. Volatile because the notification arrives on a CoreAudio thread
    // and the gate reads it on another.
    private volatile bool _muted;

    // The running IOProc, or null while stopped.
    private CoreAudioIoProcHandle? _ioProc;

    // ---- the hand-off off CoreAudio's realtime thread ----------------------------------
    //
    // The IOProc runs on an IO thread with a hard deadline: one device buffer period, about
    // 10 ms at 48 kHz, and a thread that overruns it repeatedly gets its IOProc dropped.
    // Everything behind DataAvailable was written for the Windows shape, where NAudio raises
    // on a managed thread it owns: it allocates several KB per buffer and takes locks that
    // the tap pump and the mute notification also take, so raising inline would hand
    // CoreAudio's deadline to a GC pause and to lock contention with non-realtime threads.
    //
    // So the IOProc does the one thing it must (copy out of CoreAudio's buffer, which is
    // recycled the moment it returns) into a pre-allocated slot, publishes it and returns. A
    // pump thread raises the event.
    //
    // The producer allocates nothing. It is NOT lock-free: SemaphoreSlim.Release takes the
    // semaphore's monitor to wake a parked waiter, measured at roughly 400 ns against a
    // 10,667 us deadline, and every managed wake primitive pays that. Allocation and unbounded
    // work were the hazards worth removing; a short uncontended monitor is not one.
    //
    // Hand-rolled rather than a bounded Channel, and the reason is payload ownership rather
    // than cost. The producer cannot allocate a byte[] per buffer, so slots have to be a ring
    // either way, and a slot cannot be reused until the handler for it RETURNS, which is a
    // hand-back edge a Channel has no concept of. Its DropWrite also reports fullness after
    // the write, where this has to know before it stamps a slot. A Channel would replace the
    // semaphore and leave the ring, the pre-check and the hand-back in place.
    private const int RingSlots = 8;

    // A slot holds one device buffer, not one second of audio. CoreAudio's period is a few ms
    // (512 frames is typical, 4096 the usual ceiling), so 8192 frames is double the largest
    // period a device is expected to ask for. Sizing by SampleRate instead put every slot over
    // the 85 KB large-object threshold: 384 KB each and 3 MB per capture at 48 kHz stereo, and
    // 98 MB on a 64-channel aggregate, all of it pinned in the LOH for the meeting. The depth
    // that actually matters is RingSlots (8 buffers of pump slack), which slot size does not
    // affect.
    private const int MaxBufferFrames = 8192;
    private long _dropped;              // producer-only, diagnostic; spans generations

    // The running generation, or null while stopped. Volatile because the IO thread reads it
    // and Stop clears it; null is also what tells a callback arriving after teardown that
    // there is nowhere left to put it.
    private volatile Ring? _active;
    private Thread? _pump;
    private CancellationTokenSource? _pumping;

    /// <summary>One Start's worth of hand-off: the slots, their lengths, the counters and the
    /// semaphore that publishes them.
    ///
    /// Scoped to the generation rather than held in fields on the capture, because
    /// <see cref="StopPump"/>'s join is BOUNDED. A pump abandoned at the cap is still inside
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

    /// <summary>How long <see cref="Stop"/> waits for the pump to leave before abandoning it.
    /// Sized like its siblings (<c>TapSession.DisposeDrainTimeout</c>,
    /// <c>CaptureOrchestrator.AbandonTeardownCap</c>): long enough that an ordinary handler
    /// finishes, short enough that the tray's own quit budget still fits around it. Overrunning
    /// it abandons a background thread that can raise at most the handler it is already
    /// inside.</summary>
    private static readonly TimeSpan PumpStopCap = TimeSpan.FromSeconds(2);

    public AudioFormat Format { get; }

    /// <summary>True while the endpoint is muted at the OS level. Permanently false on a
    /// device that carries no mute property, matching the Windows sibling: the mic still
    /// records, it just cannot honour an OS mute, and there the level gate is the only mute
    /// (#159).</summary>
    public bool IsMuted => _muted;

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    public event EventHandler? MuteChanged;

    public event EventHandler<Exception?>? Failed;

    /// <summary>Open <paramref name="deviceId"/> for capture. The stream format is read
    /// eagerly, so a layout the resampler cannot read surfaces from construction rather than
    /// mid-stream, and a throw here leaves this instance owning nothing.</summary>
    /// <param name="hal">The facade over CoreAudio. Owned by the enumerator that handed it
    /// over, which outlives every capture it opens, so this class never releases it.</param>
    /// <param name="deviceId">The device's <c>AudioObjectID</c>.</param>
    /// <exception cref="NotSupportedException">The device's stream layout is unreadable.
    /// </exception>
    /// <exception cref="CoreAudioException">The stream format could not be read.</exception>
    public MacOSAudioCapture(ICoreAudioHal hal, uint deviceId)
    {
        ArgumentNullException.ThrowIfNull(hal);
        _hal = hal;
        _deviceId = deviceId;
        // Classify FIRST: an unreadable layout throws, and doing it before anything is
        // subscribed means the throw leaves this instance owning nothing to release. Same
        // ordering, for the same reason, as the Windows sibling's ctor.
        Format = CoreAudioFormat.Classify(hal.ReadStreamFormat(deviceId));

        if (hal.TryReadMute(deviceId) is null)
            return;

        // Subscribe BEFORE seeding, so a toggle during construction is not lost in the gap;
        // the seed then reads the reconciled current state.
        _muteListener = hal.AddPropertyListener(deviceId, CoreAudioPropertyKind.Mute, OnMuteProperty);
        try
        {
            _muted = hal.TryReadMute(deviceId) ?? false;
        }
        catch
        {
            // The seed is the one native call this ctor makes AFTER taking ownership of
            // something, and it can fail on its own (the property is there, the read is
            // refused). Undo the subscription before the throw leaves: nobody will ever hold
            // this instance, so nobody can Dispose it, and a listener left behind is a native
            // registration plus the GCHandle rooting it for the process lifetime - still
            // firing into a half-constructed capture.
            _muteListener.Dispose();
            throw;
        }
    }

    // Fires on a CoreAudio notification thread. The device's whole notification set reaches
    // one listener, so a volume tweak arrives here too; forward only true mute transitions,
    // or a volume slider churns the pipeline.
    private void OnMuteProperty()
    {
        bool muted = _hal.TryReadMute(_deviceId) ?? false;
        if (muted == _muted)
            return;
        _muted = muted;
        MuteChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>Buffers CoreAudio delivered that the pump never got to, because it was still
    /// behind when the ring filled. Non-zero means the machine could not keep up; the count is
    /// what a later slice would surface rather than guess at.</summary>
    internal long DroppedBuffers => Interlocked.Read(ref _dropped);

    /// <summary>Whether a pump thread is running for this capture right now. The generation is
    /// the pump's only root, so this is also the answer to "did a failed Start leave a thread
    /// parked on a semaphore nobody will release", which is otherwise observable only as a
    /// collectability question.</summary>
    internal bool IsPumping => _active is not null;

    // Runs on the CoreAudio IO thread, once per buffer. Allocation-free: everything it touches
    // was sized in Start. Its one blocking call is the semaphore release, which is bounded and
    // measured (see the note on the fields above).
    private void OnIoProc(ReadOnlySpan<byte> audio)
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
                DataAvailable?.Invoke(
                    this, new AudioCapturedEventArgs(ring.Slots[slot].AsMemory(0, ring.Lengths[slot])));
                Volatile.Write(ref ring.Read, ring.Read + 1);
            }
        }
        catch (OperationCanceledException)
        {
            // Stop or Dispose cancelled the wait, which is the only way out of the loop.
        }
    }

    public void Start()
    {
        // InvalidOperationException, not the native failure type: a double start is a bug in
        // the caller rather than a dead endpoint, so the orchestrator's skip-and-carry-on
        // filter must not swallow it. Guarding here also keeps a second registration from
        // overwriting the handle below and leaking the first.
        if (_ioProc is not null)
            throw new InvalidOperationException($"device {_deviceId} is already capturing");

        // Everything the IO thread will touch, allocated before it can run. A slot holds one
        // device buffer period, so the producer's over-length drop is a guard rather than a
        // working path on any device that asks for a sane one.
        _dropped = 0;
        var ring = new Ring(MaxBufferFrames * Format.BytesPerInterleavedFrame);
        var pumping = new CancellationTokenSource();
        // Started BEFORE the IOProc, so the first buffer has somewhere to go. IsBackground so
        // a pump nobody stopped cannot hold the process up.
        var pump = new Thread(() => PumpLoop(ring, pumping.Token))
        {
            IsBackground = true,
            Name = $"tapscribe-capture-{_deviceId}",
        };
        _active = ring;
        _pumping = pumping;
        _pump = pump;
        pump.Start();

        // BOTH native calls inside the guard, because the pump is already running: a
        // CreateIoProc that refuses leaves a thread parked on a semaphore nothing will ever
        // release, holding its ring, and Dispose cannot collect it either since it releases
        // through _ioProc, which a failed Start never assigns. The tray retries a device that
        // refused, so that is a thread and a ring per attempt for the process lifetime.
        CoreAudioIoProcHandle? ioProc = null;
        try
        {
            ioProc = _hal.CreateIoProc(_deviceId, OnIoProc);
            _hal.StartIo(ioProc);
        }
        catch
        {
            try
            {
                // Registered but not running. Unregister before letting the failure out: the
                // tray retries a device that refused, so keeping it would leak one
                // registration per attempt for the process lifetime. Assigning _ioProc only
                // AFTER the start succeeds is the other half - a failed Start leaves this
                // instance holding nothing, so a later Stop has nothing to release and
                // announces nothing.
                if (ioProc is not null)
                    _hal.DestroyIoProc(ioProc);
            }
            catch (CoreAudioException)
            {
                // The device that just refused to start is refusing this too, which is what a
                // half-gone endpoint does. Swallowed so it cannot mask the start failure the
                // caller filters on; what is lost is one registration on a device already
                // failing, which goes when the device does.
            }
            finally
            {
                // In a finally, and after the IOProc is down rather than before, for the two
                // reasons ReleaseIoProc gives: a live IOProc must never outlive the pump it
                // publishes into, and the pump must come down on EVERY way out of here or the
                // thread and its ring outlive the capture nobody will ever hold.
                StopPump();
            }

            throw;
        }

        _ioProc = ioProc;
    }

    public void Stop()
    {
        // Announced only when this call is what ENDED a running stream. A blind Stop from a
        // teardown path, or a second one, released nothing, so there is no end of stream to
        // report and a Failed here would have the pipeline announce a device that never ran.
        if (ReleaseIoProc())
            Failed?.Invoke(this, null);
    }

    public void Dispose()
    {
        try
        {
            ReleaseIoProc();
        }
        catch (Exception ex) when (ex is ExternalException or InvalidOperationException)
        {
            // ExternalException: the endpoint was invalidated while capture ran (unplugged,
            // disabled, default switched), so CoreAudio refuses to stop what is already gone.
            // InvalidOperationException: the HAL was released first, so it no longer holds the
            // registration this is handing back - an ownership order the seam forbids, but one
            // a teardown path must survive rather than diagnose. Swallowed because Dispose is
            // contract-bound not to throw: every teardown path reaches it from a finally or
            // from the tray's bounded Quit, and a throw here strands the device AND skips the
            // listener detach below for the process lifetime. What is lost is the report,
            // which Stop would have propagated to an owner that could still act on it. The
            // filter is the same one TapSession.DisposeAsync applies to Stop.
        }

        // Detaching is what stops a late notification landing mid-teardown.
        _muteListener?.Dispose();
        GC.SuppressFinalize(this);
    }

    // Stop and unregister the IOProc, in that order: CoreAudio refuses to destroy a running
    // one. Returns whether there was one to release, which is what tells a clean stop apart
    // from a stop that stopped nothing. Deliberately does NOT raise Failed: Dispose releases
    // through here too, and by then the owner has let go of the events, so a signal raised
    // there has nobody to act on it.
    private bool ReleaseIoProc()
    {
        CoreAudioIoProcHandle? ioProc = _ioProc;
        if (ioProc is null)
            return false;
        _ioProc = null;

        try
        {
            _hal.StopIo(ioProc);
        }
        finally
        {
            try
            {
                _hal.DestroyIoProc(ioProc);
            }
            catch (CoreAudioException)
            {
                // The endpoint is gone, so there is no registration left for CoreAudio to
                // remove and it says so. Swallowed rather than propagated because it would
                // mask whatever StopIo reported, which is the failure the caller can actually
                // act on; nothing is lost, since a registration on a dead device dies with it.
            }
            // After the IOProc is down, never before: the producer writes to the ring and
            // releases the semaphore, so tearing this down first would leave a live IO thread
            // publishing into a pump that has gone.
            StopPump();
        }

        return true;
    }

    // Cancel the pump and wait for it to leave. Cancelling makes its next Wait throw, so it
    // raises nothing further; a handler already in flight still runs to completion, which is
    // what the seam's "the buffer is live until the handler returns" requires.
    //
    // The join is bounded rather than indefinite: a consumer that blocks forever must not take
    // the tray's teardown with it. If the cap expires the thread is abandoned, which is safe
    // because it is a background thread holding only this capture's ring, and because it can
    // raise at most the one handler it was already inside.
    private void StopPump()
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
}
