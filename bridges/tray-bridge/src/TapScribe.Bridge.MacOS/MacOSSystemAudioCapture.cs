using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS system-audio <see cref="IAudioCapture"/>: everything the Mac is playing, as one
/// stereo mixdown (#420). The other half of a meeting, and the reason the Bridge exists on
/// this platform at all - the microphone is the operator, and this is everyone else.
///
/// There is no loopback endpoint on macOS, so this is not "open a render device". It is three
/// native objects with three lifetimes, composed here:
///
/// <list type="number">
/// <item>a <b>process tap</b> over every process, which carries the audio but is not a device
/// and has no audio path of its own;</item>
/// <item>a private <b>aggregate device</b> built around the output endpoint the Mac is playing
/// through, which lists the tap and so gives it an <c>AudioObjectID</c>;</item>
/// <item>an <b>IOProc</b> on that device, which is the only one of the three that actually
/// delivers buffers.</item>
/// </list>
///
/// The first two are the constructor's, so an unusable format or a Mac that refuses the tap
/// surfaces at Open, where <c>BridgeRuntime</c> skips the device and records the meeting on
/// the microphone alone; the third is <see cref="Start"/>'s, so a stop can be followed by a
/// start without rebuilding the tap. Every native call goes through
/// <see cref="ICoreAudioHal"/>, so all of that is exercised on a lane with no audio hardware
/// and no TCC grant.
///
/// It is NOT opened against a particular render endpoint, and that is a decision rather than a
/// simplification: system audio means what the Mac is PLAYING, which by definition goes to the
/// current default output. An aggregate built around any other endpoint records silence, so
/// this finds the default itself and <see cref="Rebind"/>s when it moves - plugging in
/// headphones mid-meeting otherwise loses the far side of the call from that moment on, with
/// nothing to say so.
/// </summary>
internal sealed class MacOSSystemAudioCapture : IAudioCapture
{
    private readonly ICoreAudioHal _hal;

    // Everything behind DataAvailable runs off CoreAudio's realtime IO thread, which has a
    // buffer-period deadline this class may not spend. CaptureHandOff owns that rule.
    private readonly CaptureHandOff _handOff;

    // Fires when the Mac starts playing through a different endpoint, which is the one thing
    // that can invalidate this capture's whole binding.
    private readonly IDisposable _defaultOutputListener;

    // Serialises the binding against the tray thread. The rebind arrives on a CoreAudio
    // notification thread while Start, Stop and Dispose arrive on the tray's, and both mutate
    // the same three handles; without this an output switch during teardown releases a handle
    // the other thread is still using.
    private readonly Lock _binding = new();

    private Bound? _bound;
    private bool _streaming;
    private bool _disposed;

    /// <summary>The tap, its aggregate device and the listener watching that device leave: one
    /// value, because they are made together, released together and replaced together on a
    /// rebind, and because holding them as three fields is what makes a half-torn-down binding
    /// writable.</summary>
    /// <param name="Tap">The process tap.</param>
    /// <param name="Aggregate">The aggregate device carrying it.</param>
    /// <param name="OutputDeviceUid">The endpoint the aggregate was built around, so a
    /// notification can be answered without rebuilding anything when it has not moved.</param>
    /// <param name="Gone">The listener on the aggregate's own liveness.</param>
    private sealed record Bound(
        CoreAudioTapHandle Tap,
        CoreAudioAggregateHandle Aggregate,
        string OutputDeviceUid,
        IDisposable Gone)
    {
        /// <summary>The running IOProc, or null while stopped. Mutable because it is the one
        /// part of a binding that comes and goes with Start/Stop.</summary>
        internal CoreAudioIoProcHandle? IoProc { get; set; }
    }

    public AudioFormat Format { get; }

    /// <summary>Permanently false: a process tap is a render path and has no OS mute to
    /// honour, matching the Windows loopback sibling. The level gate is the only mute there is
    /// here (#159).</summary>
    public bool IsMuted => false;

    public event EventHandler<AudioCapturedEventArgs>? DataAvailable;

    /// <summary>Never raised. Declared because the seam does, and a tap has no mute to
    /// change.</summary>
    public event EventHandler? MuteChanged
    {
        add { }
        remove { }
    }

    public event EventHandler<Exception?>? Failed;

    /// <summary>Build the tap and the aggregate device around whatever the Mac is playing
    /// through right now. A throw leaves this instance owning nothing: the constructor hands
    /// the instance to nobody, so nobody could ever Dispose it.</summary>
    /// <param name="hal">The facade over CoreAudio. Owned by the enumerator that handed it
    /// over, which outlives every capture it opens, so this class never releases it.</param>
    /// <exception cref="NotSupportedException">The tap's stream layout is unreadable.</exception>
    /// <exception cref="CoreAudioException">This Mac has no output endpoint to tap, or refused
    /// the tap, the aggregate device or the format read.</exception>
    public MacOSSystemAudioCapture(ICoreAudioHal hal)
    {
        ArgumentNullException.ThrowIfNull(hal);
        _hal = hal;
        _handOff = new CaptureHandOff(
            "tapscribe-system-audio", audio => DataAvailable?.Invoke(this, new AudioCapturedEventArgs(audio)));

        Bound bound = Bind();
        try
        {
            // Classified from the TAP's own description, not the endpoint's: the tap is a
            // mixdown CoreAudio resamples for us, so the speakers' configuration says nothing
            // about what arrives here. An unreadable layout throws, which is what makes it an
            // Open failure the runtime can skip rather than a mid-meeting surprise.
            Format = CoreAudioFormat.Classify(hal.ReadTapFormat(bound.Tap));

            // Subscribed AFTER the format is settled, so the one call that can still throw does
            // not leave a system-wide listener behind, and BEFORE the instance escapes, so an
            // output switch during construction is not lost.
            _defaultOutputListener = hal.AddPropertyListener(
                CoreAudioObject.System, CoreAudioPropertyKind.DefaultOutputDevice, OnDefaultOutputChanged);
        }
        catch
        {
            Release(bound);
            throw;
        }

        _bound = bound;
    }

    /// <summary>The aggregate device the IOProc runs over. Internal, for the tests that push
    /// audio in as CoreAudio would: the id is not a fact any caller of the seam needs, and a
    /// rebind changes it.</summary>
    internal uint AggregateDeviceId
    {
        get { lock (_binding) return _bound?.Aggregate.DeviceId ?? 0; }
    }

    /// <summary>Whether a pump thread is running. See
    /// <see cref="CaptureHandOff.IsPumping"/>.</summary>
    internal bool IsPumping => _handOff.IsPumping;

    /// <summary>Buffers CoreAudio delivered that the pump never got to. See
    /// <see cref="CaptureHandOff.DroppedBuffers"/>.</summary>
    internal long DroppedBuffers => _handOff.DroppedBuffers;

    public void Start()
    {
        lock (_binding)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            // InvalidOperationException, not the native failure type: a double start is a bug
            // in the caller rather than a Mac that refused, so the orchestrator's
            // skip-and-carry-on filter must not swallow it.
            if (_streaming)
                throw new InvalidOperationException("system audio is already being captured");

            StartIo(_bound!);
            // Set only once the IOProc is actually running, so a refused Start leaves this
            // capture stopped and retryable rather than claiming a stream it does not have.
            _streaming = true;
        }
    }

    public void Stop()
    {
        bool ended;
        lock (_binding)
            ended = StopIo();

        // Announced outside the lock, and only when this call is what ENDED a running stream:
        // a blind Stop from a teardown path released nothing, so there is no end of stream to
        // report and a Failed here would have the pipeline announce a device that never ran.
        if (ended)
            Failed?.Invoke(this, null);
    }

    public void Dispose()
    {
        lock (_binding)
        {
            if (_disposed)
                return;
            _disposed = true;

            // Deliberately raises nothing: by the time an owner releases the capture it has
            // let go of the events, so a signal here has nobody to act on it.
            StopIo();
            if (_bound is { } bound)
                Release(bound);
            _bound = null;
        }

        // Outside the lock, because the notification it detaches can be running right now and
        // is itself waiting on the lock; detaching under it would deadlock against a rebind
        // that is mid-flight.
        _defaultOutputListener.Dispose();
        GC.SuppressFinalize(this);
    }

    // ---- binding ---------------------------------------------------------------------------

    // Build the tap and the aggregate device around the endpoint the Mac is playing through
    // now. Hands back a whole binding or releases what it made: a half-built one has no owner.
    private Bound Bind()
    {
        string outputUid = DefaultOutputUid();
        CoreAudioTapHandle tap = _hal.CreateProcessTap();
        CoreAudioAggregateHandle? aggregate = null;
        try
        {
            aggregate = _hal.CreateAggregateDevice(outputUid, tap);
            // Watched on the AGGREGATE rather than on the endpoint underneath: an aggregate
            // whose sub-device leaves is itself invalidated, so this is the one object whose
            // departure means this capture has stopped delivering.
            IDisposable gone = _hal.AddPropertyListener(
                aggregate.DeviceId, CoreAudioPropertyKind.DeviceIsAlive, OnAggregateGone);
            return new Bound(tap, aggregate, outputUid, gone);
        }
        catch
        {
            // The aggregate first when there is one, because it lists the tap: destroying the
            // tap out from under it leaves a device pointing at an object that is gone, which
            // is the ordering CoreAudio itself refuses.
            SwallowRelease(() => { if (aggregate is not null) _hal.DestroyAggregateDevice(aggregate); });
            SwallowRelease(() => _hal.DestroyProcessTap(tap));
            throw;
        }
    }

    // The endpoint the Mac is playing through, by the SAME rule a meeting's follow-default
    // selection resolves by (CaptureDevice.DefaultFor: the flagged default, else the first of
    // the flow), so the tap and the operator's device list cannot disagree about which output
    // that is.
    private string DefaultOutputUid()
    {
        IReadOnlyList<CaptureDevice> outputs =
            [.. _hal.ListDevices().Select(MacOSAudioDeviceEnumerator.Portable)];
        return CaptureDevice.DefaultFor(outputs, DeviceFlow.Render)?.Id
            // The seam's declared native failure, so the runtime skips this device and records
            // the meeting on the microphone alone rather than refusing to start at all.
            ?? throw new CoreAudioException(
                "finding an output endpoint to tap: this Mac reports none", NoOutputEndpoint);
    }

    private void Release(Bound bound)
    {
        bound.Gone.Dispose();
        SwallowRelease(() => _hal.DestroyAggregateDevice(bound.Aggregate));
        SwallowRelease(() => _hal.DestroyProcessTap(bound.Tap));
    }

    // Every release path here reaches CoreAudio for an object that may already be gone, and
    // each is called from a teardown or an unwind with no other owner to fall back on.
    private static void SwallowRelease(Action release)
    {
        try
        {
            release();
        }
        catch (CoreAudioException)
        {
            // The object is already gone, which is the state this call was trying to reach,
            // and CoreAudio says so. Swallowed rather than propagated because it would mask
            // whatever the caller is actually unwinding from; nothing is lost, since a tap or
            // an aggregate CoreAudio has forgotten dies with the process either way.
        }
        catch (InvalidOperationException)
        {
            // The HAL was released before this capture was, so it no longer holds the handle
            // being handed back: an ownership order the seam forbids, but one a teardown path
            // must survive rather than diagnose.
        }
    }

    // ---- the IOProc ------------------------------------------------------------------------

    // Caller holds the lock.
    private void StartIo(Bound bound)
    {
        // Everything the IO thread will touch, allocated before it can run, and the pump
        // started BEFORE the IOProc so the first buffer has somewhere to go.
        _handOff.Start(Format);
        CoreAudioIoProcHandle? ioProc = null;
        try
        {
            ioProc = _hal.CreateIoProc(bound.Aggregate.DeviceId, _handOff.Write);
            _hal.StartIo(ioProc);
        }
        catch
        {
            // Registered but not running: the tray retries a device that refused, so keeping it
            // would leak one registration per attempt for the process lifetime.
            if (ioProc is not null)
                SwallowRelease(() => _hal.DestroyIoProc(ioProc));
            // In a finally's place, and AFTER the IOProc is down rather than before: a live
            // IOProc must never outlive the pump it publishes into, and the pump must come
            // down on every way out of here or the thread and its ring outlive the capture.
            _handOff.Stop();
            throw;
        }

        bound.IoProc = ioProc;
    }

    // Stop and unregister the IOProc, in that order: CoreAudio refuses to destroy a running
    // one. Returns whether there was one to release, which is what tells a clean stop apart
    // from a stop that stopped nothing. Caller holds the lock.
    private bool StopIo()
    {
        _streaming = false;
        if (_bound?.IoProc is not { } ioProc)
            return false;
        _bound.IoProc = null;

        try
        {
            _hal.StopIo(ioProc);
        }
        catch (CoreAudioException)
        {
            // The aggregate was invalidated while the meeting ran, so there is nothing left to
            // stop and CoreAudio says so. Swallowed rather than propagated because every caller
            // reaches this from a teardown or a rebind that has to carry on regardless; what is
            // lost is a report about a device that is already gone.
        }
        finally
        {
            SwallowRelease(() => _hal.DestroyIoProc(ioProc));
            // After the IOProc is down, never before: the producer writes to the ring and
            // releases the semaphore, so tearing this down first would leave a live IO thread
            // publishing into a pump that has gone.
            _handOff.Stop();
        }

        return true;
    }

    // ---- what the notifications mean ---------------------------------------------------------

    // Fires on a CoreAudio notification thread when the Mac starts playing through a different
    // endpoint. Left unimplemented in this slice deliberately: what a rebind MEANS is its own
    // behaviour, and a half-done one would silently keep the old binding.
    private void OnDefaultOutputChanged()
    {
    }

    // Fires on a CoreAudio notification thread when the aggregate leaves.
    private void OnAggregateGone()
    {
    }

    // kAudioHardwareBadDeviceError, the four-char code '!dev': the platform's own word for
    // "the device you are asking about is not there".
    private const int NoOutputEndpoint = 560227702;
}
