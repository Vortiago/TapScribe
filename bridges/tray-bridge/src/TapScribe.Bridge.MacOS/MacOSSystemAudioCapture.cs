using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS system-audio <see cref="IAudioCapture"/>: everything the Mac is playing, as one
/// stereo mixdown (#420). The microphone is the operator; this is everyone else.
///
/// macOS has no loopback endpoint, so this is not "open a render device". It is three native
/// objects with three lifetimes:
///
/// <list type="number">
/// <item>a <b>process tap</b> over every process, which carries the audio but is not a device;</item>
/// <item>a private <b>aggregate device</b> around the output endpoint the Mac is playing through,
/// which lists the tap and so gives it an <c>AudioObjectID</c>;</item>
/// <item>an <b>IOProc</b> on that device, the only one of the three that delivers buffers.</item>
/// </list>
///
/// The first two are the constructor's, so a refused tap surfaces at Open, where
/// <c>BridgeRuntime</c> skips the device and records on the microphone alone; the third is
/// <see cref="Start"/>'s, so stop-then-start does not rebuild the tap.
///
/// It binds to the CURRENT default output rather than a chosen endpoint: system audio means what
/// the Mac is playing, and an aggregate built around any other endpoint records silence. It
/// <see cref="Rebind"/>s when the default moves, or plugging in headphones mid-meeting loses the
/// far side of the call with nothing to say so.
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

    // Listeners whose bindings are gone, waiting to be detached OUTSIDE the lock. Detaching one
    // under it is the deadlock: AudioObjectRemovePropertyListener waits for a callback already
    // running, and that callback's first statement takes this same lock.
    private readonly List<IDisposable> _detachOnceUnlocked = [];

    /// <summary>The tap, its aggregate device and the listener watching that device leave: one
    /// value, because they are made, released and replaced together, and because three fields is
    /// what makes a half-torn-down binding writable.</summary>
    /// <param name="OutputDeviceUid">The endpoint the aggregate was built around, so a notification
    /// about an endpoint that has not moved costs nothing.</param>
    private sealed record Bound(
        CoreAudioTapHandle Tap,
        CoreAudioAggregateHandle Aggregate,
        string OutputDeviceUid,
        IDisposable Gone,
        BindingLife Life);

    /// <summary>Whether a binding is still the current one. Its own object because the listener
    /// closes over it at registration, before the <see cref="Bound"/> exists, and because a
    /// notification that arrives after its binding was replaced must be able to say so.</summary>
    private sealed class BindingLife
    {
        // Written under the lock, read from a CoreAudio notification thread.
        private volatile bool _retired;

        internal bool Retired => _retired;

        internal void Retire() => _retired = true;
    }

    public AudioFormat Format { get; }

    /// <summary>Permanently false: a process tap is a render path with no OS mute to honour,
    /// matching the Windows loopback sibling. The level gate is the only mute here (#159).</summary>
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

    /// <summary>Build the tap and the aggregate device around whatever the Mac is playing right
    /// now. A throw leaves this instance owning nothing, since nobody can Dispose an instance the
    /// constructor never handed out.</summary>
    /// <param name="hal">The facade over CoreAudio, owned by the enumerator that handed it over,
    /// which outlives every capture it opens.</param>
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
            // From the TAP's own description, not the endpoint's: the tap is a mixdown CoreAudio
            // resamples for us, so the speakers' configuration says nothing about what arrives. An
            // unreadable layout throws here, making it an Open failure the runtime can skip.
            Format = CoreAudioFormat.Classify(hal.ReadTapFormat(bound.Tap));

            // AFTER the format is settled, so a throw leaves no system-wide listener behind, and
            // BEFORE the instance escapes, so an output switch during construction is not lost.
            _defaultOutputListener = hal.AddPropertyListener(
                CoreAudioObject.System, CoreAudioPropertyKind.DefaultOutputDevice, OnDefaultOutputChanged);
        }
        catch
        {
            Release(bound);
            Detach();
            throw;
        }

        _bound = bound;
    }

    /// <summary>The aggregate device the IOProc runs over. Internal, for the tests that push audio
    /// in as CoreAudio would; a rebind changes it.</summary>
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
            // The seam's declared type for a call in the wrong state, not the native one: nothing
            // on the Mac refused these, so a CoreAudioException would report a device fault that
            // did not happen. Both are caller bugs; the orchestrator starts each capture once.
            if (_run.Running)
                throw new InvalidOperationException("system audio is already being captured");
            if (_bound is not { } bound)
                throw new InvalidOperationException("system audio has no tap left to start");

            // A refused Start leaves the run holding nothing, so this capture stays stopped and
            // retryable rather than claiming a stream it does not have.
            _run.Start(bound.Aggregate.DeviceId, Format);
        }
    }

    public void Stop()
    {
        bool ended;
        lock (_binding)
            ended = _run.Stop();

        // Outside the lock, and only when this call ENDED a running stream: a blind Stop from a
        // teardown path released nothing, so Failed here would announce a device that never ran.
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

            // Raises nothing: an owner releasing the capture has already let go of the events.
            _run.Abandon();
            if (_bound is { } bound)
                Release(bound);
            _bound = null;
        }

        // Outside the lock: a notification either detaches may be waiting on that lock right now,
        // so detaching under it deadlocks against a mid-flight rebind.
        Detach();
        _defaultOutputListener.Dispose();
        GC.SuppressFinalize(this);
    }

    // ---- binding ---------------------------------------------------------------------------

    // Build the tap and the aggregate device around one endpoint. Hands back a whole binding or
    // releases what it made: a half-built one has no owner.
    //
    // The endpoint is a PARAMETER because both callers have already resolved it. Re-walking would
    // cost four native property reads per device for an answer in hand, and could disagree with
    // it, leaving the rebind's skip-if-unchanged decision made about a different endpoint.
    private Bound Bind(string outputUid)
    {
        CoreAudioTapHandle tap = _hal.CreateProcessTap();
        CoreAudioAggregateHandle? aggregate = null;
        try
        {
            aggregate = _hal.CreateAggregateDevice(outputUid, tap);
            // Watched on the AGGREGATE, not the endpoint underneath: an aggregate whose sub-device
            // leaves is itself invalidated, so it is the object whose departure means this capture
            // stopped delivering.
            var life = new BindingLife();
            IDisposable gone = _hal.AddPropertyListener(
                aggregate.DeviceId, CoreAudioPropertyKind.DeviceIsAlive, () => OnAggregateGone(life));
            return new Bound(tap, aggregate, outputUid, gone, life);
        }
        catch
        {
            // The aggregate first, because it lists the tap: the reverse leaves a device pointing
            // at an object that is gone, an ordering CoreAudio refuses.
            SwallowRelease(() => { if (aggregate is not null) _hal.DestroyAggregateDevice(aggregate); });
            SwallowRelease(() => _hal.DestroyProcessTap(tap));
            throw;
        }
    }

    // The endpoint the Mac is playing through, by the same rule a follow-default selection uses
    // (CaptureDevice.DefaultFor), so the tap and the operator's device list cannot disagree.
    private string DefaultOutputUid()
    {
        IReadOnlyList<CaptureDevice> outputs =
            [.. _hal.ListDevices().Select(MacOSAudioDeviceEnumerator.Portable)];
        return CaptureDevice.DefaultFor(outputs, DeviceFlow.Render)?.Id
            // The seam's declared native failure, so the runtime skips this device and records on
            // the microphone alone rather than refusing to start.
            ?? throw new CoreAudioException(
                "finding an output endpoint to tap: this Mac reports none", CoreAudioStatus.BadDevice);
    }

    // Caller holds the lock, and owes a Detach() once it has left it.
    private void Release(Bound bound)
    {
        bound.Life.Retire();
        _detachOnceUnlocked.Add(bound.Gone);
        SwallowRelease(() => _hal.DestroyAggregateDevice(bound.Aggregate));
        SwallowRelease(() => _hal.DestroyProcessTap(bound.Tap));
    }

    // Detach what Release retired, with the lock RELEASED. A handler this waits for is then free
    // to take the lock, find its binding retired and leave. Safe to call with nothing queued.
    private void Detach()
    {
        IDisposable[] listeners;
        lock (_binding)
        {
            if (_detachOnceUnlocked.Count == 0)
                return;
            listeners = [.. _detachOnceUnlocked];
            _detachOnceUnlocked.Clear();
        }

        foreach (IDisposable listener in listeners)
            listener.Dispose();
    }

    // Every release path reaches CoreAudio for an object that may already be gone, from a teardown
    // or an unwind with no other owner to fall back on.
    private static void SwallowRelease(Action release)
    {
        try
        {
            release();
        }
        catch (CoreAudioException)
        {
            // The object is already gone, which is the state this call wanted. Propagating would
            // mask whatever the caller is unwinding from, and nothing is lost: a tap CoreAudio has
            // forgotten dies with the process.
        }
        catch (InvalidOperationException)
        {
            // The HAL was released before this capture, so it no longer holds the handle: an
            // ownership order the seam forbids, but one a teardown path must survive.
        }
    }

    // ---- what the notifications mean ---------------------------------------------------------

    // Fires on a CoreAudio notification thread when the Mac starts playing through a different
    // endpoint. The aggregate is built around one endpoint and does not follow, so left alone the
    // rest of the meeting records silence while the status line still says streaming.
    private void OnDefaultOutputChanged()
    {
        Exception? failure;
        lock (_binding)
            failure = Rebind();
        Detach();

        // Outside the lock: a handler may reach back into this capture, and the pipeline's tears
        // the whole session down.
        if (failure is not null)
            Failed?.Invoke(this, failure);
    }

    // Move the tap to whatever the Mac is playing through now, keeping the stream running across
    // it. Returns what to report, or null when there was nothing to do. Caller holds the lock.
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
            // Every output left at once, or the HAL refused the walk. Reported rather than retried:
            // nothing fires this property again until an endpoint comes back.
            Unbind(current);
            return ex;
        }

        // CoreAudio fires this property on changes this capture has no stake in, and a rebind
        // rebuilds a tap and an aggregate device, dropping whatever lands in the gap.
        if (string.Equals(moved, current.OutputDeviceUid, StringComparison.Ordinal))
            return null;

        bool wasStreaming = _run.Running;
        // The old binding goes FIRST. Taps and aggregate devices are system-wide, so building the
        // replacement while the old pair is live leaves two of each on any path that throws.
        Unbind(current);

        Bound next;
        try
        {
            next = Bind(moved);
        }
        catch (Exception ex) when (CaptureSeam.IsDeclaredOpenFailure(ex))
        {
            // The endpoint moved somewhere this Mac will not tap. Bind released what it made, so
            // nothing is held here. The seam's WHOLE declared set: this runs from a CoreAudio
            // notification whose trampoline swallows escapees, so a failure this filter misses is
            // one the operator never hears about.
            return ex;
        }

        // Everything from here owns `next`, published or not: both objects are system-wide, so a
        // throw that left them unreleased strands them for the process lifetime with nothing able
        // to name them.
        try
        {
            AudioFormat arriving = CoreAudioFormat.Classify(_hal.ReadTapFormat(next.Tap));
            // Format is read once at Open and the Resampler downstream was built from it. An
            // endpoint whose tap reads differently would have these bytes reinterpreted at the
            // wrong rate and channel count, which is noise recorded as speech.
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
        catch (Exception ex) when (CaptureSeam.IsDeclaredOpenFailure(ex))
        {
            // The new tap's format is unreadable, or the endpoint refused the IOProc. Nothing is
            // left to record the far side with, so the pipeline is told rather than left with a
            // capture that reports fine and delivers nothing. Unbind covers a binding already
            // published and one that never was: it stops whatever runs, then releases the pair.
            //
            // The seam's WHOLE declared set, because `next` is owned from here: the HAL refusing a
            // handle it no longer holds arrives as InvalidOperationException, and letting that past
            // strands the pair, the leak the comment above describes.
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

    // Fires on a CoreAudio notification thread when the aggregate leaves: its sub-device went away,
    // so the device carrying the tap is invalidated and CoreAudio stops calling the IOProc. Nothing
    // to re-read: a device that ARRIVES carries no listener, so this can only mean it is gone.
    private void OnAggregateGone(BindingLife life)
    {
        lock (_binding)
        {
            // This binding was replaced while the notification was in flight, so it says nothing
            // about the one running now. Checked here rather than before the lock because the
            // retirement happens under it: a check outside could still read false and then block.
            if (life.Retired)
                return;

            // Only while a stream was running: Failed means capture ended unexpectedly MID-STREAM,
            // and a binding never started has ended nothing. The next Start fails on its own.
            if (!_run.Running || _bound is not { } bound)
                return;

            // RELEASED, not merely stopped. Only the aggregate is invalidated; the tap it listed
            // and the listener on it outlive it. Abandoning the IOProc alone leaves both registered
            // with the whole Mac, and a Start free to run over an id CoreAudio has forgotten.
            Unbind(bound);
        }
        Detach();

        // Outside the lock: the pipeline's handler tears the whole session down and may reach back
        // into this capture.
        Failed?.Invoke(this, new CoreAudioException(
            "the aggregate device carrying the system-audio tap was invalidated", CoreAudioStatus.BadDevice));
    }
}
