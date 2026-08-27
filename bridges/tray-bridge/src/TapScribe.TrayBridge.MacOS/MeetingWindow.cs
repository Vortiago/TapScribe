using AppKit;
using CoreGraphics;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// A window for one meeting: the Mac sibling of WinForms' <c>MeetingForm</c>. It renders the tested
/// Core <see cref="MeetingFormView"/> projection of a <see cref="PipelineView"/> onto a caption, a
/// read-only text view and a Copy button, and decides nothing of its own.
///
/// Two callers (#107 + #168), both through <see cref="ITrayView.OpenMeetingWindow"/>: End meeting
/// opens it and renders the finished summary, and a Past-meetings re-open leaves it Loading while a
/// <c>MeetingController</c>'s emissions arrive. It starts in that state so an empty window is never
/// on screen.
///
/// A plain object that OWNS an <see cref="NSWindow"/> rather than a subclass of one: the closed flag
/// and the <see cref="Closed"/> event are then this class's own, and there is one less NSObject in a
/// shell whose NSObjects cannot be constructed under a test host.
///
/// The summary body is painted through <see cref="SummaryAttributedText"/> over Core's
/// <see cref="SummaryLayout"/>, and only when Core says the body IS markdown
/// (<see cref="MeetingFormView.BodyIsMarkdown"/>). A raw recorder error goes through
/// <see cref="SummaryMarkdown.Plain"/> instead, so an asterisk in a path stays an asterisk.
/// </summary>
internal sealed class MeetingWindow : IMeetingWindow, IDisposable
{
    private const int Width = 560;
    private const int Height = 440;
    private const int Padding = 12;
    private const int CaptionHeight = 20;
    private const int ButtonHeight = 30;
    private const int ButtonWidth = 90;
    private const int Gap = 8;

    private readonly NSWindow _window;
    private readonly NSTextField _caption;
    private readonly NSTextView _body;
    private readonly NSButton _copy;

    // The markdown behind the rendered view: Copy hands back the source the Recorder sent, and the
    // re-render guard skips an unchanged body on a poll tick.
    private string _rawBody = "";
    private bool _closed;
    private bool _disposed;

    internal MeetingWindow()
    {
        _window = new NSWindow(
            new CGRect(0, 0, Width, Height),
            NSWindowStyle.Titled | NSWindowStyle.Closable | NSWindowStyle.Miniaturizable | NSWindowStyle.Resizable,
            NSBackingStore.Buffered,
            false);
        // This object holds the window, so AppKit must not free it on close: the runtime may
        // still render a late poll emission into it, which IsDisposed answers for.
        _window.ReleaseWhenClosed(false);
        _window.Center();

        _caption = new NSTextField
        {
            Frame = new CGRect(Padding, Height - Padding - CaptionHeight, Width - (2 * Padding), CaptionHeight),
            Editable = false,
            Selectable = false,
            Bezeled = false,
            DrawsBackground = false,
            AutoresizingMask = NSViewResizingMask.WidthSizable | NSViewResizingMask.MinYMargin,
        };

        nfloat bodyBottom = Padding + ButtonHeight + Gap;
        var scroll = new NSScrollView
        {
            Frame = new CGRect(
                Padding,
                bodyBottom,
                Width - (2 * Padding),
                Height - Padding - CaptionHeight - Gap - bodyBottom),
            HasVerticalScroller = true,
            BorderType = NSBorderType.BezelBorder,
            AutoresizingMask = NSViewResizingMask.WidthSizable | NSViewResizingMask.HeightSizable,
        };
        _body = new NSTextView(new CGRect(0, 0, scroll.ContentSize.Width, scroll.ContentSize.Height))
        {
            Editable = false,
            // Read as a document: selectable so the operator can take part of a summary
            // without the Copy button, and wrapped to the window's width.
            Selectable = true,
            AutoresizingMask = NSViewResizingMask.WidthSizable,
            Font = NSFont.SystemFontOfSize(NSFont.SystemFontSize)!,
        };
        _body.TextContainer!.WidthTracksTextView = true;
        scroll.DocumentView = _body;

        _copy = new NSButton
        {
            Frame = new CGRect(Width - Padding - ButtonWidth, Padding, ButtonWidth, ButtonHeight),
            Title = "Copy",
            BezelStyle = NSBezelStyle.Rounded,
            AutoresizingMask = NSViewResizingMask.MinXMargin | NSViewResizingMask.MaxYMargin,
        };
        _copy.Activated += OnCopy;

        NSView content = _window.ContentView!;
        content.AddSubview(_caption);
        content.AddSubview(scroll);
        content.AddSubview(_copy);

        _window.WillClose += OnWillClose;

        Apply(MeetingFormView.For(null)); // open in the Loading state
    }

    /// <summary>Raised on the main thread when the operator closes the window.</summary>
    public event Action? Closed;

    /// <summary>Whether the window is gone, so the runtime stops rendering into it.</summary>
    public bool IsDisposed => _closed;

    /// <summary>Re-render from the latest poll view, or from <c>null</c> for the
    /// pre-first-poll loading state. Pure projection through Core's
    /// <see cref="MeetingFormView"/>.</summary>
    public void Render(PipelineView? view) => Apply(MeetingFormView.For(view));

    /// <summary>Put the window on screen and bring the app forward with it. A menu-bar app is
    /// not the active one when a meeting ends, so without the activation the summary would
    /// open behind whatever the operator was reading.</summary>
    internal void Show()
    {
        NSApplication.SharedApplication.Activate();
        _window.MakeKeyAndOrderFront(null);
    }

    private void OnCopy(object? sender, EventArgs e) => Copy(_rawBody);

    private void OnWillClose(object? sender, EventArgs e)
    {
        _closed = true;
        Closed?.Invoke();
    }

    /// <summary>Close the window as if the operator had, which raises
    /// <see cref="Closed"/>.</summary>
    internal void Close() => _window.Close();

    /// <summary>Release the window and everything it draws. ReleaseWhenClosed is off, which is what
    /// lets this class read the window after AppKit has closed it, so without a release the whole
    /// graph survives every close, held by handlers that capture <c>this</c>. The Windows sibling
    /// gets this free: a non-modal Form disposes itself on close.</summary>
    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        _copy.Activated -= OnCopy;
        _window.WillClose -= OnWillClose;
        _window.Dispose();
    }

    private void Apply(MeetingFormView view)
    {
        _window.Title = view.Title;
        _caption.StringValue = view.Caption;
        if (view.Body != _rawBody)
        {
            _rawBody = view.Body;
            // Parsed only when Core says the body IS markdown: reinterpreting a raw recorder
            // error as markup turns a path with an asterisk into emphasis and eats the asterisk.
            _body.TextStorage!.SetString(SummaryAttributedText.Build(
                view.BodyIsMarkdown ? SummaryMarkdown.Parse(view.Body) : SummaryMarkdown.Plain(view.Body)));
        }
        _copy.Enabled = view.CanCopy;
    }

    private static void Copy(string text)
    {
        if (string.IsNullOrEmpty(text))
            return; // nothing to copy, and clearing the pasteboard for it would be a theft
        NSPasteboard pasteboard = NSPasteboard.GeneralPasteboard;
        pasteboard.ClearContents();
        pasteboard.SetStringForType(text, NSPasteboardType.String.GetConstant()!);
    }
}
