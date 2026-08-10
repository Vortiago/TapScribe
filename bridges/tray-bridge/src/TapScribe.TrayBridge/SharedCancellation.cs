namespace TapScribe.TrayBridge;

/// <summary>
/// A <see cref="CancellationTokenSource"/> with more than one holder, disposed by the last
/// one out. The tray's past-meeting window is the case: the WINDOW cancels the poll when it
/// closes, and the POLL LOOP reads the token until it finishes — two lifetimes that end in
/// either order. Disposing on the window's close (the obvious spelling) leaves the loop
/// holding a token whose source is gone; disposing when the loop ends leaves the window's
/// later close calling <see cref="CancellationTokenSource.Cancel"/> on a disposed source,
/// which throws. Neither party can decide alone, so counting is the whole policy.
///
/// Not thread-safe by design: every holder releases from the tray's UI thread (the poll
/// loop marshals its release through the same <see cref="SynchronizationContext"/> it
/// renders on), so an interlocked count would only disguise a caller that doesn't.
/// </summary>
internal sealed class SharedCancellation
{
    private readonly CancellationTokenSource _source = new();
    private int _holders;

    /// <param name="holders">How many parties hold the source. Each calls
    /// <see cref="Release"/> exactly once when it is done with the token.</param>
    public SharedCancellation(int holders)
    {
        ArgumentOutOfRangeException.ThrowIfLessThan(holders, 1);
        _holders = holders;
    }

    /// <summary>The token every holder observes. Throws once the last holder has released.</summary>
    public CancellationToken Token => _source.Token;

    /// <summary>Whether the last holder has released and the source is gone.</summary>
    public bool IsReleased => _holders == 0;

    /// <summary>Cancel the token — safe while any holder remains.</summary>
    public void Cancel() => _source.Cancel();

    /// <summary>One holder is done with the token. The last one out disposes the source.</summary>
    public void Release()
    {
        if (_holders == 0)
            throw new InvalidOperationException("SharedCancellation released more times than it has holders.");
        if (--_holders == 0)
            _source.Dispose();
    }
}
