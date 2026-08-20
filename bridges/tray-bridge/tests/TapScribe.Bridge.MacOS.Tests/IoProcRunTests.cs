using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The IOProc orderings, driven directly (#420). Both captures route through
/// <see cref="IoProcRun"/>, and its whole reason to exist is owning four rules that were
/// previously written twice and had drifted. A rule with one owner and no test is no better
/// than a rule with two owners: the extraction only helps if breaking it goes red.
///
/// Driven through <c>FakeCoreAudioHal</c> wrapped in a recorder, not a double of its own: these
/// assertions are about WHEN a call arrives relative to the pump, which the shared fake has no
/// way to observe, but every rule it DOES enforce still applies here. A standalone double would
/// have to restate handle validation to keep them, and the one written first did not: its
/// destroy was a no-op, so nothing here could observe a registration that CoreAudio refused to
/// destroy.
/// </summary>
public class IoProcRunTests
{
    private static readonly AudioFormat Stereo = new(48_000, 2, SampleKind.Float32);

    [Fact]
    public void Start_HasThePumpRunningBeforeTheIoProcIsRegistered()
    {
        // The ordering that had no test until this one, and the reason it matters: CoreAudio can
        // deliver the first buffer the instant the IOProc is created, and a pump that starts
        // afterwards drops whatever arrives in the gap. Silent, and it looks like a device that
        // took a moment to warm up.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff);
        var run = new IoProcRun(hal, handOff);

        run.Start(deviceId: 41, Stereo);

        Assert.True(hal.PumpingWhenCreated, "the IOProc was registered before the pump was up");
        Assert.True(hal.PumpingWhenStarted, "the IOProc was started before the pump was up");
        run.Abandon();
    }

    [Fact]
    public void Start_WhenTheDeviceRefusesTheIoProc_LeavesNoPumpRunning()
    {
        // The pump is up before the create, so every way out of Start has to take it down again.
        // Left running it is a thread parked on a semaphore nothing will release, holding its
        // ring, and nothing holds a handle to reach it: the tray retries a refused device, so
        // that is one thread and one ring per attempt for the process lifetime.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff) { CreateError = new CoreAudioException("creating", -66748) };
        var run = new IoProcRun(hal, handOff);

        Assert.Throws<CoreAudioException>(() => run.Start(deviceId: 41, Stereo));

        Assert.False(handOff.IsPumping, "a refused Start left its pump thread behind");
        Assert.False(run.Running);
    }

    [Fact]
    public void Stop_ReportsWhatCoreAudioSaidAboutTheStop()
    {
        // IAudioCapture.Stop declares ExternalException for an endpoint invalidated mid-capture
        // and says teardown swallows it. So the primitive propagates and the CALLER decides:
        // swallowing here would make every backend quietly stricter than the seam, which is the
        // divergence the two captures had before they shared this.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff) { StopError = new CoreAudioException("stopping", -66748) };
        var run = new IoProcRun(hal, handOff);
        run.Start(deviceId: 41, Stereo);

        Assert.Throws<CoreAudioException>(() => run.Stop());

        // Released regardless of what the stop reported, and the pump with it: the failure is a
        // report, not a reason to keep a registration.
        Assert.False(run.Running);
        Assert.False(handOff.IsPumping);
    }

    [Fact]
    public void Abandon_OnTheSameFailure_ReleasesWithoutThrowing()
    {
        // The other half of that decision. Dispose and a rebind have no other owner and must
        // carry on, so they take this one; it differs from Stop ONLY in what it does with the
        // report.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff) { StopError = new CoreAudioException("stopping", -66748) };
        var run = new IoProcRun(hal, handOff);
        run.Start(deviceId: 41, Stereo);

        Assert.True(run.Abandon());

        Assert.False(run.Running);
        Assert.False(handOff.IsPumping);
    }

    [Fact]
    public void Abandon_WhatItSwallows_IsStillCounted()
    {
        // Abandon reports nothing by contract, which is the right call for a path with no other
        // owner and the wrong one for a device that has quietly stayed busy since the meeting
        // ended. The counter is the only trace such a teardown leaves.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff) { StopError = new CoreAudioException("stopping", -66748) };
        var run = new IoProcRun(hal, handOff);
        run.Start(deviceId: 41, Stereo);

        run.Abandon();

        Assert.Equal(1, run.TeardownFaults);
    }

    [Fact]
    public void Release_ThatCoreAudioAccepts_CountsNoFaults()
    {
        // The other direction, so the count above cannot be "always one" and still pass.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff);
        var run = new IoProcRun(hal, handOff);
        run.Start(deviceId: 41, Stereo);

        run.Stop();

        Assert.Equal(0, run.TeardownFaults);
    }

    [Fact]
    public void Start_WhenTheHandOffRefuses_GivesTheClaimBack()
    {
        // The claim is taken before the hand-off starts, so every way OUT of Start must give it
        // back. A claim stranded here answers "already running" about a run that never began,
        // permanently, which for the system-audio capture kills every later rebind too.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff);
        var run = new IoProcRun(hal, handOff);

        // A null format is the reachable refusal: CaptureHandOff.Start null-checks it.
        Assert.Throws<ArgumentNullException>(() => run.Start(deviceId: 41, null!));

        // The claim is not observable, so assert what it gates: a real Start still works.
        run.Start(deviceId: 41, Stereo);
        Assert.True(run.Running);
        run.Abandon();
    }

    [Fact]
    public void Release_LeavesNoRegistrationBehind()
    {
        // Asserted on the registration rather than on the call order, because a leak is what an
        // inverted teardown actually produces: CoreAudio refuses to destroy an IOProc that was
        // not stopped first, and Release swallows that refusal (correctly, since a destroy can
        // also fail because the object is already gone). The refusal is therefore invisible and
        // only the surviving registration says the order was wrong.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff);
        var run = new IoProcRun(hal, handOff);
        run.Start(deviceId: 41, Stereo);

        run.Stop();

        Assert.Equal(0, hal.LiveIoProcs);
    }

    [Fact]
    public void Release_ClaimsTheHandleOnce_SoTwoCallersCannotBothStopIt()
    {
        // Stop and Dispose both reach the release, and a read-then-null lets both claim one
        // registration: CoreAudio is asked to stop and destroy it twice, and the second Stop
        // announces an end of stream that already ended.
        var handOff = new CaptureHandOff("test-pump", _ => { });
        var hal = new OrderingHal(handOff);
        var run = new IoProcRun(hal, handOff);
        run.Start(deviceId: 41, Stereo);

        Assert.True(run.Stop());
        Assert.False(run.Stop(), "a second release claimed the same IOProc");
        Assert.Equal(1, hal.Stops);
    }

    // FakeCoreAudioHal with the one thing it cannot report added: what the pump was doing at the
    // moment each call arrived, which is the only way to assert an ordering from outside. Every
    // call forwards, so the fake's handle validation still governs, including the refusal to
    // destroy an IOProc that was not stopped first.
    private sealed class OrderingHal(CaptureHandOff handOff) : ICoreAudioHal
    {
        private readonly FakeCoreAudioHal _inner = new();

        internal Exception? CreateError { get => _inner.CreateIoProcError; init => _inner.CreateIoProcError = value; }

        internal Exception? StopError { get => _inner.StopIoError; init => _inner.StopIoError = value; }

        internal int LiveIoProcs => _inner.LiveIoProcs;

        internal bool PumpingWhenCreated { get; private set; }

        internal bool PumpingWhenStarted { get; private set; }

        internal int Stops { get; private set; }

        public CoreAudioIoProcHandle CreateIoProc(uint deviceId, CoreAudioIoCallback callback)
        {
            PumpingWhenCreated = handOff.IsPumping;
            return _inner.CreateIoProc(deviceId, callback);
        }

        public void StartIo(CoreAudioIoProcHandle ioProc)
        {
            PumpingWhenStarted = handOff.IsPumping;
            _inner.StartIo(ioProc);
        }

        public void StopIo(CoreAudioIoProcHandle ioProc)
        {
            Stops++;
            _inner.StopIo(ioProc);
        }

        public void DestroyIoProc(CoreAudioIoProcHandle ioProc) => _inner.DestroyIoProc(ioProc);

        public IReadOnlyList<CoreAudioDevice> ListDevices() => _inner.ListDevices();

        public CoreAudioStreamFormat ReadStreamFormat(uint deviceId) => _inner.ReadStreamFormat(deviceId);

        public bool? TryReadMute(uint deviceId) => _inner.TryReadMute(deviceId);

        public IDisposable AddPropertyListener(uint objectId, CoreAudioPropertyKind kind, Action handler) =>
            _inner.AddPropertyListener(objectId, kind, handler);

        public CoreAudioTapHandle CreateProcessTap() => _inner.CreateProcessTap();

        public void DestroyProcessTap(CoreAudioTapHandle tap) => _inner.DestroyProcessTap(tap);

        public CoreAudioStreamFormat ReadTapFormat(CoreAudioTapHandle tap) => _inner.ReadTapFormat(tap);

        public CoreAudioAggregateHandle CreateAggregateDevice(string outputDeviceUid, CoreAudioTapHandle tap) =>
            _inner.CreateAggregateDevice(outputDeviceUid, tap);

        public void DestroyAggregateDevice(CoreAudioAggregateHandle aggregate) =>
            _inner.DestroyAggregateDevice(aggregate);

        public void Dispose() => _inner.Dispose();
    }
}
