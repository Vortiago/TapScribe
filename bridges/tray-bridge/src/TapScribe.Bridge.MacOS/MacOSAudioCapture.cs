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

    // Everything behind DataAvailable runs off CoreAudio's realtime IO thread, which has a
    // buffer-period deadline this class may not spend. CaptureHandOff owns that rule for
    // every capture in this backend; see it for why the ring, the pump and the drop are
    // shaped the way they are.
    private readonly CaptureHandOff _handOff;

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
        _handOff = new CaptureHandOff(
            $"tapscribe-capture-{deviceId}", audio => DataAvailable?.Invoke(this, new AudioCapturedEventArgs(audio)));
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

    /// <summary>Buffers CoreAudio delivered that the pump never got to. See
    /// <see cref="CaptureHandOff.DroppedBuffers"/>.</summary>
    internal long DroppedBuffers => _handOff.DroppedBuffers;

    /// <summary>Whether a pump thread is running for this capture right now. See
    /// <see cref="CaptureHandOff.IsPumping"/>.</summary>
    internal bool IsPumping => _handOff.IsPumping;

    public void Start()
    {
        // InvalidOperationException, not the native failure type: a double start is a bug in
        // the caller rather than a dead endpoint, so the orchestrator's skip-and-carry-on
        // filter must not swallow it. Guarding here also keeps a second registration from
        // overwriting the handle below and leaking the first.
        if (_ioProc is not null)
            throw new InvalidOperationException($"device {_deviceId} is already capturing");

        // Everything the IO thread will touch, allocated before it can run, and the pump
        // started BEFORE the IOProc so the first buffer has somewhere to go.
        _handOff.Start(Format);

        // BOTH native calls inside the guard, because the pump is already running: a
        // CreateIoProc that refuses leaves a thread parked on a semaphore nothing will ever
        // release, holding its ring, and Dispose cannot collect it either since it releases
        // through _ioProc, which a failed Start never assigns. The tray retries a device that
        // refused, so that is a thread and a ring per attempt for the process lifetime.
        CoreAudioIoProcHandle? ioProc = null;
        try
        {
            ioProc = _hal.CreateIoProc(_deviceId, _handOff.Write);
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
                _handOff.Stop();
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
            _handOff.Stop();
        }

        return true;
    }
}
