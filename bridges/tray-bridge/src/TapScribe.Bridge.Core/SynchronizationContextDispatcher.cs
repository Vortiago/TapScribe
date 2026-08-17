namespace TapScribe.Bridge.Core;

/// <summary>
/// An <see cref="IDispatcher"/> over a <see cref="SynchronizationContext"/>: the adapter for
/// any platform that HAS one. WinForms installs a context once its message loop is pumping,
/// so the tray shell captures it at that point and wraps it here.
///
/// There is deliberately no macOS counterpart of this class. .NET installs no
/// SynchronizationContext on macOS, so the AppKit shell implements <see cref="IDispatcher"/>
/// directly over <c>DispatchQueue.MainQueue</c> rather than manufacturing a context to wrap.
/// That asymmetry is the reason the seam is <see cref="IDispatcher"/> and not the context
/// itself.
/// </summary>
public sealed class SynchronizationContextDispatcher(SynchronizationContext context) : IDispatcher
{
    private readonly SynchronizationContext _context =
        context ?? throw new ArgumentNullException(nameof(context));

    public void Post(Action action)
    {
        ArgumentNullException.ThrowIfNull(action);
        _context.Post(static state => ((Action)state!)(), action);
    }
}
