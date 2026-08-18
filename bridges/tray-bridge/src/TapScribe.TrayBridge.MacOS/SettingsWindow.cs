using AppKit;
using CoreGraphics;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The Settings window: a two-way binding of AppKit controls onto Core's
/// <see cref="SettingsDraft"/>, which owns every decision about what the edited state MEANS.
/// The Mac sibling of WinForms' <c>SettingsForm</c>, and deliberately the smaller half of it:
/// connection, the microphone row, the shared gate timings and the end-of-meeting behaviour.
/// The Advanced pin grid and the live level meters are device parity, which slice 9 owns.
///
/// Not modal. A modal run loop would sit on the main queue that every runtime callback is
/// posted to, so a meeting running behind the window would stop being able to report anything.
/// Save applies through the runtime and closes; Cancel just closes.
/// </summary>
internal sealed class SettingsWindow
{
    private const int Width = 480;

    // The row budget, not a taste: the layout below is a top-down cursor, so this has to cover
    // every row it places (19 of them, plus a heading's extra air and the two-line notes) or
    // the last control is framed below the window and simply is not drawn. Nothing catches
    // that but opening the window, so a row added here comes with a bump to this number and a
    // look at the Save button.
    private const int Height = 760;
    private const int Padding = 16;
    private const int LabelWidth = 120;
    private const int RowHeight = 22;
    private const int Gap = 8;
    private const int ControlLeft = Padding + LabelWidth + Gap;
    private const int ControlWidth = Width - ControlLeft - Padding;

    /// <summary>How long a connection probe may take before it is abandoned. The Recorder is
    /// usually on the same machine or the same LAN, and an operator staring at a spinner
    /// learns nothing a refusal would not tell them sooner.</summary>
    private static readonly TimeSpan TestTimeout = TimeSpan.FromSeconds(15);

    private readonly NSWindow _window;
    private readonly IDispatcher _dispatcher;
    private readonly Action<BridgeSettings> _apply;
    private readonly SettingsDraft _draft;

    private readonly NSTextField _host;
    private readonly NSTextField _port;
    private readonly NSSecureTextField _token;
    private readonly NSButton _tls;
    private readonly NSButton _allowSelfSigned;
    private readonly NSButton _test;
    private readonly NSTextField _testStatus;
    private readonly NSButton _micEnabled;
    private readonly NSTextField _micName;
    private readonly NSSlider _micSensitivity;
    private readonly NSTextField _micSensitivityReadout;
    private readonly NSTextField _hangover;
    private readonly NSTextField _preRoll;
    private readonly NSButton _processOnEnd;

    private nfloat _y = Height - Padding;

    /// <summary>Build the window over the settings in force.</summary>
    /// <param name="current">What the runtime is running on, which is what the window seeds
    /// itself from: an edit that failed to reach disk still governs the session.</param>
    /// <param name="listDevices">Lists the endpoints present now, so a saved pin survives a
    /// Save. See <see cref="SettingsSeed"/> for why that matters with no pin grid on screen.</param>
    /// <param name="apply">Hands the edited settings to the runtime, which publishes,
    /// persists and re-tunes any running pipelines.</param>
    /// <param name="dispatcher">Marshals the connection test's answer back to the main thread:
    /// there is no SynchronizationContext here, so the continuation lands on a thread pool
    /// thread, and AppKit may not be touched from one.</param>
    internal SettingsWindow(
        BridgeSettings current,
        Func<IReadOnlyList<CaptureDevice>> listDevices,
        Action<BridgeSettings> apply,
        IDispatcher dispatcher)
    {
        ArgumentNullException.ThrowIfNull(current);
        ArgumentNullException.ThrowIfNull(listDevices);
        ArgumentNullException.ThrowIfNull(apply);
        ArgumentNullException.ThrowIfNull(dispatcher);
        _apply = apply;
        _dispatcher = dispatcher;
        _draft = SettingsSeed.From(current, listDevices);

        _window = new NSWindow(
            new CGRect(0, 0, Width, Height),
            NSWindowStyle.Titled | NSWindowStyle.Closable,
            NSBackingStore.Buffered,
            false)
        {
            Title = "TapScribe Bridge settings",
        };
        // This object holds the window, so AppKit must not free it on close: the shell asks
        // IsOpen afterwards to decide whether to raise it or build a new one.
        _window.ReleaseWhenClosed(false);
        _window.Center();
        NSView content = _window.ContentView!;

        Section(content, "Recorder");
        _host = Field(content, "Host", _draft.Host, ControlWidth);
        _port = Field(content, "Port", _draft.Port.ToString(System.Globalization.CultureInfo.InvariantCulture), 90);
        _token = Secret(content, "Tap token", _draft.Token);
        _tls = Check(content, "Connect over TLS", _draft.Tls);
        _allowSelfSigned = Check(content, "Accept a self-signed certificate", _draft.AllowSelfSignedCert);

        _test = Button(content, "Test connection", ControlLeft, 150);
        _test.Activated += (_, _) => _ = TestConnectionAsync();
        _testStatus = Note(content, "", lines: 2);

        Section(content, "Microphone");
        _micEnabled = Check(content, "Record my microphone", _draft.MicEnabled);
        _micName = Field(content, "Speaker name", _draft.MicName, ControlWidth);
        _micSensitivity = Slider(content, "Sensitivity", _draft.MicSensitivity);
        _micSensitivityReadout = Note(content, SettingsDraft.SensitivityLabel(_draft.MicSensitivity), lines: 1);
        _micSensitivity.Activated += (_, _) =>
            _micSensitivityReadout.StringValue = SettingsDraft.SensitivityLabel(_micSensitivity.IntValue);

        Section(content, "Speech gate");
        _hangover = Field(content, "Hangover (ms)", _draft.HangoverMs.ToString(System.Globalization.CultureInfo.InvariantCulture), 90);
        _preRoll = Field(content, "Pre-roll (ms)", _draft.PreRollMs.ToString(System.Globalization.CultureInfo.InvariantCulture), 90);

        Section(content, "Meetings");
        _processOnEnd = Check(content, "Transcribe and summarize when the meeting ends", _draft.ProcessOnEnd);

        // Said in the window rather than left for the operator to infer from a meeting that
        // reports a device it could not find: on this platform system audio is a Core Audio
        // process tap rather than a loopback endpoint, and that is #420's work. A saved
        // system-audio selection is carried through Save untouched, which is why there is no
        // control for it here rather than a disabled one claiming a state.
        Note(content, "System audio is not captured on macOS yet, so a meeting records this Mac's microphone only.", lines: 2);

        NSButton save = Button(content, "Save", Width - Padding - 100, 100);
        save.Activated += (_, _) => Save();
        save.KeyEquivalent = "\r"; // Return saves, the way a Mac dialog's default button does
        NSButton cancel = Button(content, "Cancel", Width - Padding - 100 - Gap - 100, 100, sameRow: true);
        cancel.Activated += (_, _) => Close();
        cancel.KeyEquivalent = ""; // Escape closes without applying
    }

    /// <summary>Whether the window is still on screen, so a second Settings… raises this one
    /// rather than stacking another.</summary>
    internal bool IsOpen => _window.IsVisible;

    /// <summary>Put the window on screen and bring the app forward with it: a menu-bar app is
    /// not the active one when its menu is clicked.</summary>
    internal void Show()
    {
        NSApplication.SharedApplication.Activate();
        _window.MakeKeyAndOrderFront(null);
    }

    /// <summary>Close the window without applying anything.</summary>
    internal void Close() => _window.Close();

    private void Save()
    {
        _apply(Collect());
        Close();
    }

    // Read every control back into the draft and let Core collect it. The draft is the one
    // that knows what a ticked box means for the device selection, which pins survive and
    // which gate a device keeps.
    private BridgeSettings Collect()
    {
        _draft.Host = _host.StringValue.Trim();
        _draft.Port = SettingsFields.Int(_port.StringValue, _draft.Port, min: 1, max: 65535);
        _draft.Token = _token.StringValue;
        _draft.Tls = IsOn(_tls);
        _draft.AllowSelfSignedCert = IsOn(_allowSelfSigned);
        _draft.MicEnabled = IsOn(_micEnabled);
        _draft.MicName = _micName.StringValue;
        _draft.MicSensitivity = _micSensitivity.IntValue;
        _draft.HangoverMs = SettingsFields.Int(_hangover.StringValue, _draft.HangoverMs, min: 0, max: 5000);
        _draft.PreRollMs = SettingsFields.Int(_preRoll.StringValue, _draft.PreRollMs, min: 0, max: 2000);
        _draft.ProcessOnEnd = IsOn(_processOnEnd);
        return _draft.ToSettings();
    }

    // The same probe the SpatialChat bridge's popup runs: reachability, then a /tap handshake
    // under the reserved __probe__ identity that ConnectionTester owns. Collect() first, so
    // the test asks about what is typed rather than about what was last saved.
    private async Task TestConnectionAsync()
    {
        _test.Enabled = false;
        _testStatus.StringValue = "Testing…";
        TapConnectionOptions options = Collect().ToConnectionOptions();

        string outcome;
        try
        {
            using var timeout = new CancellationTokenSource(TestTimeout);
            ConnectionTestResult result = await ConnectionTester
                .TestAsync(options, http: null, timeout.Token)
                .ConfigureAwait(false);
            outcome = result.Describe();
        }
        catch (Exception ex) when (ex is not OutOfMemoryException)
        {
            // Deliberately the widest filter in this project, for the same reason
            // BridgeSettingsStore's token read has one. ConnectionTester answers a bad host, a
            // refused token and a timeout as RESULTS, so what is left here is whatever a
            // malformed entry makes some layer below throw, and this runs fire-and-forget from
            // a click: anything escaping would be swallowed by the task scheduler, leaving the
            // button dead and the operator with no answer at all. What is lost is the stack,
            // and the message is what they could have acted on anyway.
            outcome = $"Test failed: {ex.Message}";
        }

        // Back to the main thread before touching either control: the await above resumed on a
        // thread pool thread, because macOS has no SynchronizationContext to capture.
        _dispatcher.Post(() =>
        {
            _testStatus.StringValue = outcome;
            _test.Enabled = true;
        });
    }

    // ---- Layout ---------------------------------------------------------------------------
    // A top-down cursor rather than constraints: the window is fixed-size and single-column,
    // so the frames say what they mean and there is no layout pass to reason about.

    private static bool IsOn(NSButton check) => check.State == NSCellStateValue.On;

    private nfloat NextRow(nfloat height)
    {
        _y -= height;
        nfloat top = _y;
        _y -= Gap;
        return top;
    }

    private void Section(NSView content, string title)
    {
        _y -= Gap; // a little air above each heading
        nfloat y = NextRow(RowHeight);
        content.AddSubview(new NSTextField
        {
            Frame = new CGRect(Padding, y, Width - (2 * Padding), RowHeight),
            StringValue = title,
            Font = NSFont.BoldSystemFontOfSize(NSFont.SystemFontSize)!,
            Editable = false,
            Selectable = false,
            Bezeled = false,
            DrawsBackground = false,
        });
    }

    private void Caption(NSView content, string text, nfloat y)
    {
        content.AddSubview(new NSTextField
        {
            Frame = new CGRect(Padding, y, LabelWidth, RowHeight),
            StringValue = text,
            Alignment = NSTextAlignment.Right,
            Editable = false,
            Selectable = false,
            Bezeled = false,
            DrawsBackground = false,
        });
    }

    private NSTextField Field(NSView content, string label, string value, nfloat width)
    {
        nfloat y = NextRow(RowHeight);
        Caption(content, label, y);
        var field = new NSTextField
        {
            Frame = new CGRect(ControlLeft, y, width, RowHeight),
            StringValue = value,
        };
        content.AddSubview(field);
        return field;
    }

    private NSSecureTextField Secret(NSView content, string label, string value)
    {
        nfloat y = NextRow(RowHeight);
        Caption(content, label, y);
        var field = new NSSecureTextField
        {
            Frame = new CGRect(ControlLeft, y, ControlWidth, RowHeight),
            StringValue = value,
        };
        content.AddSubview(field);
        return field;
    }

    private NSButton Check(NSView content, string title, bool on)
    {
        nfloat y = NextRow(RowHeight);
        var check = new NSButton
        {
            Frame = new CGRect(ControlLeft, y, ControlWidth, RowHeight),
            Title = title,
        };
        check.SetButtonType(NSButtonType.Switch);
        check.State = on ? NSCellStateValue.On : NSCellStateValue.Off;
        content.AddSubview(check);
        return check;
    }

    private NSSlider Slider(NSView content, string label, int value)
    {
        nfloat y = NextRow(RowHeight);
        Caption(content, label, y);
        var slider = new NSSlider
        {
            Frame = new CGRect(ControlLeft, y, 240, RowHeight),
            MinValue = 0,
            MaxValue = 100,
            IntValue = value,
            Continuous = true,
        };
        content.AddSubview(slider);
        return slider;
    }

    private NSTextField Note(NSView content, string text, int lines)
    {
        nfloat height = RowHeight * lines;
        nfloat y = NextRow(height);
        var note = new NSTextField
        {
            Frame = new CGRect(ControlLeft, y, Width - ControlLeft - Padding, height),
            StringValue = text,
            Editable = false,
            Selectable = false,
            Bezeled = false,
            DrawsBackground = false,
            Font = NSFont.SystemFontOfSize(NSFont.SmallSystemFontSize)!,
        };
        note.Cell!.Wraps = true;
        content.AddSubview(note);
        return note;
    }

    private NSButton Button(NSView content, string title, nfloat x, nfloat width, bool sameRow = false)
    {
        // sameRow puts a second button beside the one just placed, which is what a
        // Cancel/Save pair is: one row, two frames.
        nfloat y = sameRow ? _y + Gap : NextRow(RowHeight + 6);
        var button = new NSButton
        {
            Frame = new CGRect(x, y, width, RowHeight + 6),
            Title = title,
            BezelStyle = NSBezelStyle.Rounded,
        };
        content.AddSubview(button);
        return button;
    }
}
