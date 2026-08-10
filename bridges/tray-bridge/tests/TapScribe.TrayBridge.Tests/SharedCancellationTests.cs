namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Pins B9 — the past-meeting window's cancellation has two holders whose lifetimes end in
/// either order, and the shell used to Cancel and Dispose in the same breath when the window
/// closed, while the poll loop was still inside <c>MeetingViewDriver.DriveAsync</c> holding
/// its token. That survived only because Cancel happened to run before Dispose; any later
/// touch of the token raises ObjectDisposedException, which is not in the driver's catch
/// filter and would escape the shell's fire-and-forget discard with nothing to report it.
///
/// The rule is "the last holder out disposes", and both halves of it matter: dispose too
/// early and the poll loop holds a dead source; dispose too late (i.e. never) and the source
/// leaks. These drive <see cref="SharedCancellation"/> directly — it is the whole decision,
/// lifted out of a closure so it can be asserted rather than reasoned about.
/// </summary>
public class SharedCancellationTests
{
    [Fact]
    public void Release_WhileAnotherHolderRemains_LeavesTheTokenUsable()
    {
        // The window closes first: it cancels, then lets go. The poll loop is still running
        // and must be able to keep observing the token it was handed.
        var cancellation = new SharedCancellation(holders: 2);
        CancellationToken token = cancellation.Token;

        cancellation.Cancel();
        cancellation.Release();

        Assert.False(cancellation.IsReleased);
        Assert.True(token.IsCancellationRequested);
        Assert.True(cancellation.Token.CanBeCanceled); // the source is still there to be read
    }

    [Fact]
    public void Release_ByTheLastHolder_DisposesTheSource()
    {
        // ...and once the loop lets go too, nothing holds the source and it is released
        // rather than leaked.
        var cancellation = new SharedCancellation(holders: 2);
        cancellation.Release();
        cancellation.Release();

        Assert.True(cancellation.IsReleased);
        Assert.Throws<ObjectDisposedException>(() => cancellation.Token);
    }

    [Fact]
    public void Cancel_AfterTheOtherHolderReleased_DoesNotThrow()
    {
        // The other order: the poll loop finishes first (the meeting reached its summary and
        // the window is still open), and the operator closes the window later. Cancelling
        // then is the throw the old spelling was one scheduling accident away from.
        var cancellation = new SharedCancellation(holders: 2);
        cancellation.Release(); // the poll loop is done

        cancellation.Cancel();  // the window closes afterwards
        cancellation.Release();

        Assert.True(cancellation.IsReleased);
    }

    [Fact]
    public void Release_MoreTimesThanItHasHolders_Throws()
    {
        // A holder that releases twice would dispose the source out from under the other
        // one — exactly the bug, re-introduced by a miscount. Fail loudly instead.
        var cancellation = new SharedCancellation(holders: 1);
        cancellation.Release();

        Assert.Throws<InvalidOperationException>(cancellation.Release);
    }
}
