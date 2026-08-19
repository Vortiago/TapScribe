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

    // Fires when the endpoint leaves. Subscribed for the capture's whole life rather than per
    // Start, so the ctor's one unwind covers both listeners; whether a notification MEANS
    // anything is the handler's business, and it is only ever true mid-stream.
    private readonly IDisposable _lifeListener;

    // Cached so a read from the IO thread never re-enters CoreAudio; refreshed from the
    // property notification. Volatile because the notification arrives on a CoreAudio thread
    // and the gate reads it on another.
    private volatile bool _muted;

    // The IOProc and the ordering rules around it, shared with the system-audio capture.
    private readonly IoProcRun _run;


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
        _run = new IoProcRun(hal, _handOff);
        // Classify FIRST: an unreadable layout throws, and doing it before anything is
        // subscribed means the throw leaves this instance owning nothing to release. Same
        // ordering, for the same reason, as the Windows sibling's ctor.
        Format = CoreAudioFormat.Classify(hal.ReadStreamFormat(deviceId));

        _lifeListener = hal.AddPropertyListener(deviceId, CoreAudioPropertyKind.DeviceIsAlive, OnDeviceGone);
        try
        {
            if (hal.TryReadMute(deviceId) is null)
                return;

            // Subscribe BEFORE seeding, so a toggle during construction is not lost in the
            // gap; the seed then reads the reconciled current state.
            _muteListener = hal.AddPropertyListener(deviceId, CoreAudioPropertyKind.Mute, OnMuteProperty);
            _muted = hal.TryReadMute(deviceId) ?? false;
        }
        catch
        {
            // Everything after the first subscription runs inside this, because from there on
            // a throw would leave the ctor owning something. Nobody will ever hold this
            // instance, so nobody can Dispose it, and a listener left behind is a native
            // registration plus the GCHandle rooting it for the process lifetime - still
            // firing into a half-constructed capture. The seeding read is the shape that
            // actually happens: the mute property is there, and reading it is refused.
            _muteListener?.Dispose();
            _lifeListener.Dispose();
            throw;
        }
    }

    // Fires on a CoreAudio notification thread when the endpoint leaves: unplugged, disabled,
    // or an interface that went to sleep. CoreAudio simply stops calling the IOProc, so without
    // this the meeting records nothing under this speaker for the rest of the call while the
    // status line still says it is streaming.
    private void OnDeviceGone()
    {
        // Only while a stream is actually running. The seam says Failed means capture ended
        // unexpectedly MID-STREAM, and a device that leaves while nothing is capturing has
        // ended no stream; the next Start fails on its own, which is where that belongs.
        if (!_run.Running)
            return;

        // Deliberately releases nothing: the endpoint is gone, so stopping and destroying
        // its IOProc is a call CoreAudio will refuse, and the owner's response to this signal
        // is to tear the pipeline down through Dispose anyway - which is where that release
        // happens, once, on the thread that owns it.
        Failed?.Invoke(this, new CoreAudioException(
            $"the endpoint behind device {_deviceId} was invalidated", CoreAudioStatus.BadDevice));
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
        // filter must not swallow it.
        if (_run.Running)
            throw new InvalidOperationException($"device {_deviceId} is already capturing");

        _run.Start(_deviceId, Format);
    }

    public void Stop()
    {
        // Announced only when this call is what ENDED a running stream. A blind Stop from a
        // teardown path, or a second one, released nothing, so there is no end of stream to
        // report and a Failed here would have the pipeline announce a device that never ran.
        if (_run.Stop())
            Failed?.Invoke(this, null);
    }

    public void Dispose()
    {
        // Abandon rather than Stop: this path has no other owner and Dispose is contract-bound
        // not to throw, so it takes the release that carries on regardless. Bare rather than
        // wrapped, because that guarantee is IoProcRun's to make and it makes it; a catch here
        // would be the second owner of a policy the extraction exists to give one.
        _run.Abandon();

        // Detaching is what stops a late notification landing mid-teardown.
        _muteListener?.Dispose();
        _lifeListener.Dispose();
        GC.SuppressFinalize(this);
    }

}
