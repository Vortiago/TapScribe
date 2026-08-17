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
    // recycled the moment it returns) into a pre-allocated slot, publishes the index and
    // returns. A pump thread raises the event. The producer allocates nothing, waits on
    // nothing, and its only shared write is one Volatile.Write.
    private const int RingSlots = 8;
    private byte[][] _ring = [];
    private readonly int[] _lengths = new int[RingSlots];
    private long _written;              // producer-only, published with Volatile.Write
    private long _read;                 // pump-only, published with Volatile.Write
    private long _dropped;              // producer-only, diagnostic
    private SemaphoreSlim? _filled;     // one release per published slot
    private Thread? _pump;
    private CancellationTokenSource? _pumping;

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
        _muted = hal.TryReadMute(deviceId) ?? false;
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
    internal long Dropped => Interlocked.Read(ref _dropped);

    // Runs on the CoreAudio IO thread, once per buffer. Allocation-free and lock-free by
    // construction: everything it touches was sized in Start.
    private void OnIoProc(ReadOnlySpan<byte> audio)
    {
        SemaphoreSlim? filled = _filled;
        if (filled is null)
            return;

        // Full ring means the pump is behind. Drop THIS buffer rather than overwrite one the
        // pump has not read: overwriting would hand it a slot changing underneath it, and
        // blocking would miss the deadline, which is the one thing this thread must not do.
        if (_written - Volatile.Read(ref _read) >= RingSlots)
        {
            _dropped++;
            return;
        }

        int slot = (int)(_written % RingSlots);
        byte[] target = _ring[slot];
        // Larger than any buffer Start sized for. Dropping beats allocating on this thread.
        if (audio.Length > target.Length)
        {
            _dropped++;
            return;
        }

        audio.CopyTo(target);
        _lengths[slot] = audio.Length;
        Volatile.Write(ref _written, _written + 1);
        filled.Release();
    }

    // The pump. Raises DataAvailable off the IO thread, one buffer at a time and in order, so
    // the seam's "the buffer is reusable once the handler returns" still holds: a slot is only
    // returned to the producer after the handler for it has returned.
    private void PumpLoop(SemaphoreSlim filled, CancellationToken stopping)
    {
        try
        {
            while (true)
            {
                filled.Wait(stopping);
                int slot = (int)(_read % RingSlots);
                DataAvailable?.Invoke(
                    this, new AudioCapturedEventArgs(_ring[slot].AsMemory(0, _lengths[slot])));
                Volatile.Write(ref _read, _read + 1);
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

        // Everything the IO thread will touch, allocated before it can run. The slot size is
        // a whole second of this device's format, which no sane buffer period approaches, so
        // the producer's over-length drop is a guard rather than a working path.
        _written = 0;
        _read = 0;
        _dropped = 0;
        int slotBytes = Format.SampleRate * Format.BytesPerInterleavedFrame;
        _ring = new byte[RingSlots][];
        for (int i = 0; i < RingSlots; i++)
            _ring[i] = new byte[slotBytes];

        var filled = new SemaphoreSlim(0);
        var pumping = new CancellationTokenSource();
        // Started BEFORE the IOProc, so the first buffer has somewhere to go. IsBackground so
        // a pump nobody stopped cannot hold the process up.
        var pump = new Thread(() => PumpLoop(filled, pumping.Token))
        {
            IsBackground = true,
            Name = $"tapscribe-capture-{_deviceId}",
        };
        _filled = filled;
        _pumping = pumping;
        _pump = pump;
        pump.Start();

        CoreAudioIoProcHandle ioProc = _hal.CreateIoProc(_deviceId, OnIoProc);
        try
        {
            _hal.StartIo(ioProc);
        }
        catch
        {
            StopPump();
            // Registered but not running. Unregister before letting the failure out: the tray
            // retries a device that refused, so keeping it would leak one registration per
            // attempt for the process lifetime. Assigning _ioProc only AFTER the start
            // succeeds is the other half - a failed Start leaves this instance holding
            // nothing, so a later Stop has nothing to release and announces nothing.
            try
            {
                _hal.DestroyIoProc(ioProc);
            }
            catch (CoreAudioException)
            {
                // The device that just refused to start is refusing this too, which is what a
                // half-gone endpoint does. Swallowed so it cannot mask the start failure the
                // caller filters on; what is lost is one registration on a device already
                // failing, which goes when the device does.
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
        catch (CoreAudioException)
        {
            // The endpoint was invalidated while capture ran (unplugged, disabled, default
            // switched), so CoreAudio refuses to stop what is already gone. Swallowed because
            // Dispose is contract-bound not to throw: every teardown path reaches it from a
            // finally or from the tray's bounded Quit, and a throw here strands the device for
            // the process lifetime. What is lost is the report, which Stop would have
            // propagated to an owner that could still act on it.
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

    // Cancel the pump and wait for it to leave, so no DataAvailable can land after the call
    // that stopped the capture returns. Bounded: the pump's only blocking wait is the one
    // being cancelled, so it exits promptly or the process is in a state a longer wait would
    // not fix.
    private void StopPump()
    {
        _pumping?.Cancel();
        _filled = null;
        _pump?.Join(TimeSpan.FromSeconds(2));
        _pumping?.Dispose();
        _pumping = null;
        _pump = null;
        _ring = [];
    }
}
