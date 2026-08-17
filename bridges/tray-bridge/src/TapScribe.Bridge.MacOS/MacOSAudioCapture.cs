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

    // One reusable buffer behind every DataAvailable. The HAL hands out a span onto
    // CoreAudio's own buffer, which is recycled the moment the callback returns, and the seam
    // takes a ReadOnlyMemory - so exactly one copy is unavoidable, and this is where the
    // decision to make it a REUSED copy lives. It needs no synchronisation: CoreAudio serves
    // one device's IOProc from a single IO thread, and the seam declares the buffer reusable
    // as soon as the handler returns, which is what the reference pipeline honours by
    // consuming it synchronously.
    private byte[] _scratch = [];

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

    // Runs on the CoreAudio IO thread, once per buffer.
    private void OnIoProc(ReadOnlySpan<byte> audio)
    {
        EventHandler<AudioCapturedEventArgs>? handlers = DataAvailable;
        if (handlers is null)
            return;

        if (_scratch.Length < audio.Length)
            _scratch = new byte[audio.Length];
        audio.CopyTo(_scratch);
        handlers(this, new AudioCapturedEventArgs(_scratch.AsMemory(0, audio.Length)));
    }

    public void Start()
    {
        _ioProc = _hal.CreateIoProc(_deviceId, OnIoProc);
        _hal.StartIo(_ioProc);
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
        }

        return true;
    }
}
