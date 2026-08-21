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

    // The IOProc and the ordering rules around it, shared with the microphone capture.
    private readonly IoProcRun _run;

    // Fires when the Mac starts playing through a different endpoint, which is the one thing
    // that can invalidate this capture's whole binding.
    private readonly IDisposable _defaultOutputListener;

    // Serialises the binding against the tray thread. The rebind arrives on a CoreAudio
    // notification thread while Start, Stop and Dispose arrive on the tray's, and both mutate
    // the same three handles; without this an output switch during teardown releases a handle
    // the other thread is still using.
    private readonly Lock _binding = new();

    private Bound? _bound;
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
        IDisposable Gone);

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
        _run = new IoProcRun(hal, _handOff);

        Bound bound = Bind(DefaultOutputUid());
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


    public void Start()
    {
        lock (_binding)
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            // The seam's declared type for a call in the wrong state, not the native one:
            // nothing on the Mac refused either of these, so a CoreAudioException would report
            // a device fault the device does not have. Both are caller bugs, and neither is
            // reachable from the orchestrator, which starts each capture once.
            if (_run.Running)
                throw new InvalidOperationException("system audio is already being captured");
            if (_bound is not { } bound)
                throw new InvalidOperationException("system audio has no tap left to start");

            // A refused Start leaves the run holding nothing, so this capture stays stopped
            // and retryable rather than claiming a stream it does not have.
            _run.Start(bound.Aggregate.DeviceId, Format);
        }
    }

    public void Stop()
    {
        bool ended;
        lock (_binding)
            ended = _run.Stop();

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
            _run.Abandon();
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

    // Build the tap and the aggregate device around one endpoint. Hands back a whole binding or
    // releases what it made: a half-built one has no owner.
    //
    // The endpoint is a PARAMETER rather than resolved here, because both callers have already
    // resolved it: a rebind compares the moved-to endpoint against the binding it holds before
    // deciding to rebuild at all, and a second walk would be another four native property reads
    // per device for an answer it has - and one that can disagree with the first, leaving the
    // rebind's skip-if-unchanged decision made about a different endpoint than it binds to.
    private Bound Bind(string outputUid)
    {
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
                "finding an output endpoint to tap: this Mac reports none", CoreAudioStatus.BadDevice);
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

    // ---- what the notifications mean ---------------------------------------------------------

    // Fires on a CoreAudio notification thread when the Mac starts playing through a different
    // endpoint. The aggregate is built AROUND one endpoint and does not follow, so left alone
    // the rest of the meeting records as silence while the status line still says streaming:
    // plugging in headphones mid-call is the everyday way that happens.
    private void OnDefaultOutputChanged()
    {
        Exception? failure;
        lock (_binding)
            failure = Rebind();

        // Outside the lock: a handler is entitled to reach back into this capture, and the
        // pipeline's is the one that tears the whole session down.
        if (failure is not null)
            Failed?.Invoke(this, failure);
    }

    // Move the tap to whatever the Mac is playing through now, keeping the stream running
    // across it. Returns what to report, or null when there was nothing to do or nothing went
    // wrong. Caller holds the lock.
    private Exception? Rebind()
    {
        if (_disposed || _bound is not { } current)
            return null;

        string moved;
        try
        {
            moved = DefaultOutputUid();
        }
        catch (CoreAudioException ex)
        {
            // Every output left at once, or the HAL refused the walk. Reported rather than
            // retried: nothing will fire this property again until an endpoint comes back, and
            // the binding it names is already the wrong one.
            Unbind(current);
            return ex;
        }

        // CoreAudio fires this property on changes this capture has no stake in, and a rebind
        // is not free: it destroys and rebuilds a tap and an aggregate device, and drops
        // whatever lands in the gap. Rebuilding on every notification would punch a hole in
        // the recording each time something else on the Mac touched the property.
        if (string.Equals(moved, current.OutputDeviceUid, StringComparison.Ordinal))
            return null;

        bool wasStreaming = _run.Running;
        // The old binding goes FIRST, before anything of the new one exists. A tap is a
        // system-wide object and an aggregate device is registered with the whole Mac, so
        // building the replacement while the old pair is still live would leave two of each on
        // any path that then throws.
        Unbind(current);

        Bound next;
        try
        {
            next = Bind(moved);
        }
        catch (Exception ex) when (CaptureSeam.IsDeclaredFailure(ex))
        {
            // The endpoint moved somewhere this Mac will not tap. Bind hands back a whole
            // binding or releases what it made, so there is nothing held here.
            //
            // The seam's whole declared set, not a subset of it: this runs from a CoreAudio
            // notification, whose trampoline swallows anything that escapes, so a failure this
            // filter misses is one the operator never hears about at all.
            return ex;
        }

        // Everything from here owns `next`, published or not: a tap is a system-wide object
        // and an aggregate device is registered with the whole Mac, so a throw that left it
        // unreleased would strand both for the process lifetime with nothing able to name
        // them. Same shape as the constructor's own release-and-rethrow around this exact
        // pair of calls.
        try
        {
            AudioFormat arriving = CoreAudioFormat.Classify(_hal.ReadTapFormat(next.Tap));
            // Format is read once, at Open, and the Resampler downstream was built from it and
            // cannot be told otherwise mid-stream. An endpoint whose tap reads differently
            // would have these bytes reinterpreted at the wrong rate and channel count, which
            // is noise recorded as speech, so this refuses rather than carrying on.
            if (arriving != Format)
            {
                Release(next);
                return new NotSupportedException(
                    $"System audio moved to an endpoint whose tap is {arriving.SampleRate} Hz, " +
                    $"{arriving.Channels} channel(s), and this meeting is recording " +
                    $"{Format.SampleRate} Hz, {Format.Channels} channel(s). End the meeting and " +
                    "start it again to record from it.");
            }

            _bound = next;
            if (wasStreaming)
            {
                _run.Start(next.Aggregate.DeviceId, Format);
            }
        }
        catch (Exception ex) when (CaptureSeam.IsDeclaredFailure(ex))
        {
            // The new tap's format could not be read or is unreadable, or the endpoint refused
            // the IOProc on it. There is nothing left to record the far side with, so the
            // pipeline is told rather than left with a capture that reports fine and delivers
            // nothing. Unbind covers both halves: a binding already published (the start threw)
            // and one that never was (the format read threw), since it stops whatever is
            // running and then releases the pair either way.
            //
            // The seam's whole declared set, because `next` is owned from here on: the HAL
            // refusing a handle it no longer holds arrives as InvalidOperationException, and a
            // filter that let that one past would strand a tap and an aggregate device with
            // nothing able to name them - the exact leak the comment above this try describes.
            Unbind(next);
            return ex;
        }

        return null;
    }

    // Stop whatever this binding is doing and release it, leaving this capture holding
    // nothing. Caller holds the lock.
    private void Unbind(Bound bound)
    {
        _run.Abandon();
        Release(bound);
        _bound = null;
    }

    // Fires on a CoreAudio notification thread when the aggregate leaves: its sub-device went
    // away, so the device carrying the tap is invalidated and CoreAudio simply stops calling
    // the IOProc. Nothing is re-read, because there is nothing to re-read - a device that
    // ARRIVES carries no listener yet, so a notification reaching this one can only mean the
    // object it names is gone.
    private void OnAggregateGone()
    {
        lock (_binding)
        {
            // Only while a stream was actually running. The seam says Failed means capture
            // ended unexpectedly MID-STREAM, and a binding that was never started has ended
            // nothing; the next Start fails on its own, which is where that belongs.
            if (!_run.Running || _bound is not { } bound)
                return;

            // RELEASED, not merely stopped. Only the aggregate is invalidated: the tap it
            // listed is a separate system-wide object that outlives it, and so is the listener
            // watching the dead device. Abandoning the IOProc alone leaves both registered with
            // the whole Mac for the rest of the meeting, and leaves a Start free to run over an
            // aggregate id CoreAudio has forgotten. Unbind is what a refused rebind does with
            // the same binding, for the same reason.
            Unbind(bound);
        }

        // Outside the lock: the pipeline's handler is the one that tears the whole session
        // down, and it is entitled to reach back into this capture.
        Failed?.Invoke(this, new CoreAudioException(
            "the aggregate device carrying the system-audio tap was invalidated", CoreAudioStatus.BadDevice));
    }
}
