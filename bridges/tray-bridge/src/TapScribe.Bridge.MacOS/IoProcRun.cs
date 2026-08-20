using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// One IOProc running over one device id, and the hand-off it publishes into.
///
/// The peer of <see cref="CaptureHandOff"/>, and extracted for the same reason: both macOS
/// captures need this identically while what they run it OVER differs completely (one a real
/// endpoint, one an aggregate device wrapping a process tap). Written twice it had already
/// drifted, in two ways nobody chose, which is what an ordering rule with no single owner does.
///
/// Four orderings live here and nowhere else. Each is one line away from a bug the fake
/// refuses but only in the class that got it right:
/// <list type="bullet">
/// <item>The pump starts BEFORE the IOProc, so the first buffer has somewhere to go.</item>
/// <item>Create and start are one guarded pair: a create that refuses would otherwise leave a
/// pump parked on a semaphore nothing will release, holding its ring.</item>
/// <item>The handle is claimed atomically, because Stop and Dispose both reach here and a
/// read-then-null lets both claim it: CoreAudio is then asked to stop and destroy one
/// registration twice, and the second stop announces an end of stream that already ended.</item>
/// <item>The pump comes down AFTER the IOProc, never before: the producer writes to the ring
/// and releases the semaphore, so the reverse leaves a live IO thread publishing into a pump
/// that has gone.</item>
/// </list>
/// </summary>
/// <param name="hal">The facade over CoreAudio. Owned by whoever built this.</param>
/// <param name="handOff">The ring and pump this run's callback publishes into.</param>
internal sealed class IoProcRun(ICoreAudioHal hal, CaptureHandOff handOff)
{
    private CoreAudioIoProcHandle? _ioProc;

    private long _teardownFaults;

    /// <summary>How many teardown calls this run swallowed. Both release paths are bound not
    /// to throw, so a registration CoreAudio refused to give up leaves no other trace: the run
    /// still reports a clean release, and the leak shows up only as a device that stays busy.
    /// The counterpart of <c>CaptureHandOff.HandlerFaults</c> and
    /// <c>CoreAudioHal.CallbackFaults</c>, for the same reason.</summary>
    internal long TeardownFaults => Interlocked.Read(ref _teardownFaults);

    /// <summary>Whether an IOProc is registered and running right now. Read from CoreAudio
    /// notification threads as well as the tray thread, hence the volatile read.</summary>
    internal bool Running => Volatile.Read(ref _ioProc) is not null;

    /// <summary>Start an IOProc over <paramref name="deviceId"/>, leaving nothing behind on any
    /// failure.</summary>
    /// <param name="deviceId">The device to run over: a real endpoint, or an aggregate wrapping
    /// a process tap.</param>
    /// <param name="format">The device format the hand-off sizes its ring from.</param>
    /// <exception cref="ExternalException">CoreAudio refused the registration or the start.
    /// </exception>
    internal void Start(uint deviceId, AudioFormat format)
    {
        handOff.Start(format);
        CoreAudioIoProcHandle? ioProc = null;
        try
        {
            ioProc = hal.CreateIoProc(deviceId, handOff.Write);
            hal.StartIo(ioProc);
        }
        catch
        {
            // Registered but not running. Unregistered before the failure leaves, because the
            // tray retries a device that refused and keeping it would leak one registration per
            // attempt for the process lifetime. Assigning the field only AFTER the start
            // succeeds is the other half: a failed Start leaves this holding nothing, so a
            // later Stop releases nothing and announces nothing.
            if (ioProc is not null)
                Swallow(() => hal.DestroyIoProc(ioProc));
            handOff.Stop();
            throw;
        }

        // CompareExchange rather than a plain write: a second Start over a live run would
        // strand the first registration, its pinned callback, its pump thread and its ring for
        // the process lifetime, with a live IO thread still writing into the orphaned one. Both
        // callers guard before reaching here, so a non-null return is a bug in this class's
        // owner rather than a state a device can produce.
        if (Interlocked.CompareExchange(ref _ioProc, ioProc, null) is not null)
        {
            // Releasing what THIS call made before complaining about it. The registration is
            // live and started at this point and the field belongs to the winner, so leaving it
            // would strand exactly what the CompareExchange is here to prevent - the difference
            // being that the leak would be the loser's rather than the winner's. Swallowed the
            // way every other release path here is; the caller's bug is what propagates.
            Swallow(() => hal.StopIo(ioProc));
            Swallow(() => hal.DestroyIoProc(ioProc));
            throw new InvalidOperationException("an IOProc is already running for this capture");
        }
    }

    /// <summary>Stop and unregister the IOProc, reporting what CoreAudio said about the stop.
    /// </summary>
    /// <returns>Whether there was a running IOProc to release, which is what tells a clean stop
    /// apart from a stop that stopped nothing.</returns>
    /// <exception cref="ExternalException">The endpoint was invalidated while capture ran, so
    /// there was nothing left to stop. Propagated rather than swallowed because
    /// <see cref="IAudioCapture.Stop"/> declares exactly this and says teardown swallows it and
    /// releases the device anyway: a backend that swallows it here is quietly stricter than the
    /// seam, and a caller that wanted to know can never learn.</exception>
    internal bool Stop() => Release(propagate: true);

    /// <summary>Release the IOProc on a path that must carry on regardless: a teardown with no
    /// other owner, or a rebind that has a new binding to build. Never throws.</summary>
    /// <returns>Whether there was a running IOProc to release.</returns>
    internal bool Abandon() => Release(propagate: false);

    private bool Release(bool propagate)
    {
        CoreAudioIoProcHandle? ioProc = Interlocked.Exchange(ref _ioProc, null);
        if (ioProc is null)
            return false;

        try
        {
            hal.StopIo(ioProc);
        }
        catch (Exception ex) when (!propagate && ex is ExternalException or InvalidOperationException)
        {
            // Abandon's half of the split: the caller has no other owner and must carry on, so
            // the report goes nowhere but the counter. Stop's half lets the same failure out,
            // because IAudioCapture.Stop declares it.
            Interlocked.Increment(ref _teardownFaults);
        }
        finally
        {
            // Swallowed either way: a failed destroy means the registration is already gone,
            // and letting it out would mask whatever the stop reported, which is the failure a
            // caller can actually act on.
            Swallow(() => hal.DestroyIoProc(ioProc));
            handOff.Stop();
        }

        return true;
    }

    // The two shapes a release can meet: the platform refusing an object it no longer has, and
    // the HAL refusing a handle it no longer holds because its own Dispose swept it first.
    // Neither leaves anything to do, and both reach here from paths bound not to throw.
    //
    // Filtered on ExternalException rather than CoreAudioException, which is the type the SEAM
    // declares: CoreAudioException exists only because ExternalException is what IAudioCapture
    // names. Filtering at the concrete type would let a different ExternalException out of a
    // method documented never to throw, and Abandon's callers reach it from Dispose.
    private void Swallow(Action release)
    {
        try
        {
            release();
        }
        catch (Exception ex) when (ex is ExternalException or InvalidOperationException)
        {
            Interlocked.Increment(ref _teardownFaults);
        }
    }
}
