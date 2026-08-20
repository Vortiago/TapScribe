using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// One IOProc running over one device id, and the hand-off it publishes into.
///
/// Shared by both macOS captures, which need it identically over different devices (a real
/// endpoint, or an aggregate wrapping a process tap). Written twice, it had drifted.
///
/// Five orderings, each one line from a bug and each with a test that goes red if the line
/// moves. See <c>IoProcRunTests</c>:
/// <list type="bullet">
/// <item>Pump up before the IOProc: the first buffer needs somewhere to go.</item>
/// <item>Create and start are one guarded pair: a refused create would leave a pump parked on
/// a semaphore nothing releases.</item>
/// <item>The handle is claimed atomically: Stop and Dispose both reach the release.</item>
/// <item>The run is claimed before the hand-off is touched.</item>
/// <item>Pump down after the IOProc: the reverse leaves a live IO thread writing into a pump
/// that has gone.</item>
/// </list>
/// </summary>
/// <param name="hal">The facade over CoreAudio. Owned by whoever built this.</param>
/// <param name="handOff">The ring and pump this run's callback publishes into.</param>
internal sealed class IoProcRun(ICoreAudioHal hal, CaptureHandOff handOff)
{
    private CoreAudioIoProcHandle? _ioProc;

    // Held for the whole of Start, not just its last statement. The handle field cannot carry
    // this: it is only publishable once CoreAudio has issued a registration.
    private int _claimed;

    private long _teardownFaults;

    /// <summary>How many teardown calls this run swallowed. Both release paths are bound not to
    /// throw, so a refused release would otherwise show up only as a device that stays busy.
    /// Peer of <c>CaptureHandOff.HandlerFaults</c>.</summary>
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
    /// <exception cref="InvalidOperationException">A run is already claimed. Both callers guard
    /// before reaching here, so this is a bug in this class's owner rather than a state a device
    /// can produce.</exception>
    internal void Start(uint deviceId, AudioFormat format)
    {
        // Claimed before anything is touched: starting the hand-off replaces the generation a
        // live pump is reading, so a guard after it could only report the damage.
        if (Interlocked.CompareExchange(ref _claimed, 1, 0) != 0)
            throw new InvalidOperationException("an IOProc is already running for this capture");

        CoreAudioIoProcHandle? ioProc = null;
        try
        {
            // Inside the try: it allocates and starts a thread, so it can refuse, and the
            // claim has to come back. Stop() is null-safe on a half-built generation.
            handOff.Start(format);
            ioProc = hal.CreateIoProc(deviceId, handOff.Write);
            hal.StartIo(ioProc);
        }
        catch
        {
            // The tray retries a refused device, so an un-destroyed registration leaks one per
            // attempt. The field is never published, so only this path can clean up.
            if (ioProc is not null)
                Swallow(() => hal.DestroyIoProc(ioProc));
            handOff.Stop();
            Volatile.Write(ref _claimed, 0);
            throw;
        }

        // Plain write: the claim makes this single-entry. Volatile for Running's readers.
        Volatile.Write(ref _ioProc, ioProc);
    }

    /// <summary>Stop and unregister the IOProc, reporting what CoreAudio said about the stop.
    /// </summary>
    /// <returns>Whether there was a running IOProc to release, which is what tells a clean stop
    /// apart from a stop that stopped nothing.</returns>
    /// <exception cref="ExternalException">The endpoint was invalidated mid-capture. Propagated,
    /// not swallowed: <see cref="IAudioCapture.Stop"/> declares it, so swallowing here would make
    /// this backend quietly stricter than the seam.</exception>
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
            // Abandon's half: no other owner, so the report goes to the counter. Stop lets it out.
            Interlocked.Increment(ref _teardownFaults);
        }
        finally
        {
            // Swallowed either way: it would mask whatever the stop reported.
            Swallow(() => hal.DestroyIoProc(ioProc));
            handOff.Stop();
            // Last, so the next Start cannot touch a generation still coming down.
            Volatile.Write(ref _claimed, 0);
        }

        return true;
    }

    // Two shapes: the platform refusing an object it no longer has, and the HAL refusing a
    // handle its own Dispose swept first. Neither leaves anything to do. Filtered on the type
    // the SEAM declares, not CoreAudioException, or a different ExternalException escapes a
    // method documented never to throw.
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
