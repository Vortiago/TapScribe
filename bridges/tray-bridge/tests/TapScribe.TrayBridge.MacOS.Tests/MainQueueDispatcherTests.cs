using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS.Tests;

/// <summary>
/// The Mac shell's <see cref="IDispatcher"/> (#419). Everything the meeting runtime shows the
/// operator arrives through it, so the one thing it must never do is run the work where it
/// stands: a runtime continuation calling back into the view inline would reach AppKit off the
/// main thread, and a caller that IS the main thread would re-enter mid-callback. The queue
/// itself is AppKit's, so the seam this drives is the enqueue, which is what the production
/// constructor binds to <c>DispatchQueue.MainQueue</c>.
/// </summary>
public class MainQueueDispatcherTests
{
    [Fact]
    public void Post_HandsTheWorkToTheQueueRatherThanRunningItInline()
    {
        // The whole contract. An implementation that shortcut to "already on the main thread,
        // so just call it" would pass every other check here and still break the ordering the
        // runtime relies on, so the queueing is asserted as an absence: Post returns with the
        // work not yet run.
        Action? queued = null;
        bool ran = false;
        var dispatcher = new MainQueueDispatcher(work => queued = work);

        dispatcher.Post(() => ran = true);

        Assert.False(ran);
        Assert.NotNull(queued);
        queued();
        Assert.True(ran);
    }

    [Fact]
    public void Post_QueuesEveryCallInTheOrderItWasMade()
    {
        // The runtime posts a status and then the menu state that goes with it; a dispatcher
        // that coalesced or reordered them would leave the two disagreeing on screen.
        var queued = new List<Action>();
        var order = new List<string>();
        var dispatcher = new MainQueueDispatcher(queued.Add);

        dispatcher.Post(() => order.Add("first"));
        dispatcher.Post(() => order.Add("second"));
        foreach (Action work in queued)
            work();

        Assert.Equal(["first", "second"], order);
    }

    [Fact]
    public void Post_WithNoWork_Throws()
    {
        var dispatcher = new MainQueueDispatcher(_ => { });

        Assert.Throws<ArgumentNullException>(() => dispatcher.Post(null!));
    }
}
