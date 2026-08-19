using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The IOProc orderings, driven directly (#420). Both captures route through
/// <see cref="IoProcRun"/>, and its whole reason to exist is owning four rules that were
/// previously written twice and had drifted. A rule with one owner and no test is no better
/// than a rule with two owners: the extraction only helps if breaking it goes red.
///
/// Driven through a purpose-built double rather than <c>FakeCoreAudioHal</c>, because these
/// assertions are about WHEN a call happens relative to the pump, which the shared fake has no
/// way to observe.
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

    // Records what the pump was doing at the moment each call arrived, which is the only way to
    // assert an ordering from outside.
    private sealed class OrderingHal(CaptureHandOff handOff) : ICoreAudioHal
    {
        internal CoreAudioException? CreateError { get; init; }

        internal CoreAudioException? StopError { get; init; }

        internal bool PumpingWhenCreated { get; private set; }

        internal bool PumpingWhenStarted { get; private set; }

        internal int Stops { get; private set; }

        public CoreAudioIoProcHandle CreateIoProc(uint deviceId, CoreAudioIoCallback callback)
        {
            PumpingWhenCreated = handOff.IsPumping;
            return CreateError is null ? new Handle() : throw CreateError;
        }

        public void StartIo(CoreAudioIoProcHandle ioProc) => PumpingWhenStarted = handOff.IsPumping;

        public void StopIo(CoreAudioIoProcHandle ioProc)
        {
            Stops++;
            if (StopError is not null)
                throw StopError;
        }

        public void DestroyIoProc(CoreAudioIoProcHandle ioProc)
        {
        }

        // Not reached: this double exists for the IOProc lifecycle alone, and a call landing
        // here means a test is asking IoProcRun to do something outside its job.
        public IReadOnlyList<CoreAudioDevice> ListDevices() => throw new NotSupportedException();

        public CoreAudioStreamFormat ReadStreamFormat(uint deviceId) => throw new NotSupportedException();

        public bool? TryReadMute(uint deviceId) => throw new NotSupportedException();

        public IDisposable AddPropertyListener(uint objectId, CoreAudioPropertyKind kind, Action handler) =>
            throw new NotSupportedException();

        public CoreAudioTapHandle CreateProcessTap() => throw new NotSupportedException();

        public void DestroyProcessTap(CoreAudioTapHandle tap) => throw new NotSupportedException();

        public CoreAudioStreamFormat ReadTapFormat(CoreAudioTapHandle tap) => throw new NotSupportedException();

        public CoreAudioAggregateHandle CreateAggregateDevice(string outputDeviceUid, CoreAudioTapHandle tap) =>
            throw new NotSupportedException();

        public void DestroyAggregateDevice(CoreAudioAggregateHandle aggregate) => throw new NotSupportedException();

        public void Dispose()
        {
        }

        private sealed class Handle : CoreAudioIoProcHandle;
    }
}
