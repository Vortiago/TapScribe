using System.Globalization;
using AppKit;
using AVFoundation;
using CoreGraphics;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge.MacOS;

/// <summary>
/// The Settings window: a two-way binding of AppKit controls onto Core's
/// <see cref="SettingsDraft"/>, which owns every decision about what the edited state MEANS. The
/// Mac sibling of WinForms' <c>SettingsForm</c>, at parity with it since #421 and laid out as one
/// scrolling column rather than four tabs: Recorder, the microphone and system-audio rows with a
/// live level meter each, the Devices pin grid, the speech-gate timings and the end-of-meeting
/// behaviour.
///
/// Not modal: a modal run loop would sit on the main queue every runtime callback is posted to, so
/// a meeting running behind the window could report nothing. Save applies through the runtime and
/// closes; Cancel just closes.
///
/// Disposable for the reason <see cref="MeetingWindow"/> is: ReleaseWhenClosed is off, so without
/// a release each Settings… leaves the whole control graph behind, on a tray that runs for days.
/// </summary>
internal sealed class SettingsWindow : IDisposable
{
    private const int Width = 480;

    // How tall the window OPENS, which is taste rather than a budget: the rows live in a flipped
    // document view inside a scroll view, so the content is as tall as it needs to be and
    // anything past the viewport scrolls.
    private const int Height = 700;
    private const int Padding = 16;
    private const int LabelWidth = 120;
    private const int RowHeight = 22;
    private const int Gap = 8;
    private const int ControlLeft = Padding + LabelWidth + Gap;
    private const int ControlWidth = Width - ControlLeft - Padding;

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
    private readonly NSButton _systemEnabled;
    private readonly NSTextField _systemName;
    private readonly NSSlider _systemSensitivity;
    private readonly NSTextField _systemSensitivityReadout;
    private readonly NSTextField _hangover;
    private readonly NSTextField _preRoll;
    private readonly NSButton _processOnEnd;

    // One entry per row the draft offered, paired with the row it edits. The draft's rows are
    // the model; these are only the controls over them.
    private readonly List<(PinnedDeviceRow Row, NSButton Pin, NSTextField Name)> _pinRows = [];

    private readonly MeterProbe _micProbe;
    private readonly MeterProbe _systemProbe;
    private readonly NSButton _micMeterOn;
    private readonly NSButton _systemMeterOn;
    private readonly LevelMeterView _micMeter;
    private readonly LevelMeterView _systemMeter;
    private readonly NSTextField _micMeterNote;
    private readonly NSTextField _systemMeterNote;
    private NSTimer? _meterTimer;
    private readonly NSButton _save;
    private readonly NSButton _cancel;

    // Grows downward: the document view is flipped, so y is a distance from the TOP.
    private nfloat _y = Padding;

    // Where NextRow last placed a row, so a second control can join it without restating the
    // first one's height and misaligning the day that height changes.
    private nfloat _lastRowTop;
    private bool _disposed;

    /// <summary>Build the window over the settings in force.</summary>
    /// <param name="current">What the runtime is running on, which is what the window seeds
    /// itself from: an edit that failed to reach disk still governs the session.</param>
    /// <param name="listDevices">Lists the endpoints present now, which fills the pin grid and
    /// is what makes a saved pin survive a Save. See <see cref="SettingsSeed"/>.</param>
    /// <param name="apply">Hands the edited settings to the runtime, which publishes,
    /// persists and re-tunes any running pipelines.</param>
    /// <param name="dispatcher">Marshals the connection test's answer back to the main thread:
    /// there is no SynchronizationContext here, so the continuation lands on a thread pool
    /// thread, and AppKit may not be touched from one.</param>
    internal SettingsWindow(
        BridgeSettings current,
        Func<IReadOnlyList<CaptureDevice>> listDevices,
        Func<IAudioDeviceEnumerator> openEnumerator,
        Action<BridgeSettings> apply,
        IDispatcher dispatcher)
    {
        ArgumentNullException.ThrowIfNull(current);
        ArgumentNullException.ThrowIfNull(listDevices);
        ArgumentNullException.ThrowIfNull(openEnumerator);
        ArgumentNullException.ThrowIfNull(apply);
        ArgumentNullException.ThrowIfNull(dispatcher);
        _apply = apply;
        _dispatcher = dispatcher;
        _draft = SettingsSeed.From(current, listDevices);
        // CaptureDevice.DefaultFor, not a comparison written here: the meter must sample the
        // endpoint the gate it is tuning will tap, and re-deriving that rule is how they drift.
        _micProbe = new MeterProbe(
            openEnumerator, devices => CaptureDevice.DefaultFor(devices, DeviceFlow.Capture));
        _systemProbe = new MeterProbe(
            openEnumerator, devices => CaptureDevice.DefaultFor(devices, DeviceFlow.Render));

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

        // Every row goes into a flipped document view inside a scroll view. Flipped so the
        // top-down cursor can place a row without knowing the total height, which is what lets the
        // pin grid be as long as the operator's Mac requires. Its real height is set below.
        var content = new FlippedView { Frame = new CGRect(0, 0, Width, Height) };
        var scroll = new NSScrollView(new CGRect(0, 0, Width, Height))
        {
            HasVerticalScroller = true,
            // Never horizontal: the rows are laid out to a fixed Width, so a horizontal
            // scroller would only ever appear to say the arithmetic was wrong.
            HasHorizontalScroller = false,
            AutohidesScrollers = true,
            DrawsBackground = false,
            DocumentView = content,
        };
        _window.ContentView = scroll;

        Section(content, "Recorder");
        _host = Field(content, "Host", _draft.Host, ControlWidth);
        _port = Field(content, "Port", _draft.Port.ToString(CultureInfo.InvariantCulture), 90);
        _token = Secret(content, "Tap token", _draft.Token);
        _tls = Check(content, "Connect over TLS", _draft.Tls);
        _allowSelfSigned = Check(content, "Accept a self-signed certificate", _draft.AllowSelfSignedCert);
        // The insecure opt-in only means anything under TLS, and SettingsDraft.ToSettings drops it
        // when TLS is off. Shown rather than applied silently: an operator who ticks it over an
        // unticked TLS would otherwise find it unticked next time with nothing having said why.
        // The WinForms sibling greys and force-clears it the same way.
        _tls.Activated += OnTlsToggled;
        ApplyTlsCoupling();

        _test = Button(content, "Test connection", ControlLeft, 150);
        _test.Activated += OnTest;
        _testStatus = Note(content, "", lines: 2);

        Section(content, "Microphone");
        _micEnabled = Check(content, "Record my microphone", _draft.MicEnabled);
        _micName = Field(content, "Speaker name", _draft.MicName, ControlWidth);
        _micSensitivity = Slider(content, "Sensitivity", _draft.MicSensitivity);
        _micSensitivityReadout = Note(content, SettingsDraft.SensitivityLabel(_draft.MicSensitivity), lines: 1);
        _micSensitivity.Activated += OnMicSensitivity;
        (_micMeterOn, _micMeter, _micMeterNote) = MeterRow(content, "Show input level");
        _micMeterOn.Activated += OnMicMeterToggled;

        Section(content, "System audio");
        _systemEnabled = Check(content, "Record what this Mac plays", _draft.SystemEnabled);
        _systemName = Field(content, "Speaker name", _draft.SystemName, ControlWidth);
        _systemSensitivity = Slider(content, "Sensitivity", _draft.SystemSensitivity);
        _systemSensitivityReadout =
            Note(content, SettingsDraft.SensitivityLabel(_draft.SystemSensitivity), lines: 1);
        _systemSensitivity.Activated += OnSystemSensitivity;
        (_systemMeterOn, _systemMeter, _systemMeterNote) = MeterRow(content, "Show output level");
        _systemMeterOn.Activated += OnSystemMeterToggled;
        // The one thing about this row an operator cannot discover by looking: the grant is asked
        // for at the first Start, so a meeting that records one speaker is usually a dismissed
        // prompt. Said here because the recovery is in System Settings, not this dialog.
        Note(
            content,
            "macOS asks for permission the first time a meeting records system audio. If only "
            + "your own voice is recorded, allow TapScribe under System Settings \u203a Privacy "
            + "& Security \u203a Screen & System Audio Recording.",
            lines: 3);

        Section(content, "Devices");
        // Follow-default is the norm and needs no row: the two sections above already say
        // "record my microphone" and "record what this Mac plays", each binding late at Start.
        // This grid is for the operator who wants a SPECIFIC endpoint every time.
        Note(
            content,
            "The sections above follow whatever this Mac is using when a meeting starts. Pin a "
            + "device here to always record that one instead.",
            lines: 2);
        BuildPinGrid(content);

        Section(content, "Speech gate");
        _hangover = Field(content, "Hangover (ms)", _draft.HangoverMs.ToString(CultureInfo.InvariantCulture), 90);
        _preRoll = Field(content, "Pre-roll (ms)", _draft.PreRollMs.ToString(CultureInfo.InvariantCulture), 90);

        Section(content, "Meetings");
        _processOnEnd = Check(content, "Transcribe and summarize when the meeting ends", _draft.ProcessOnEnd);

        Section(content, "Permissions");
        PermissionRows(content);

        _save = Button(content, "Save", Width - Padding - 100, 100);
        _save.Activated += OnSave;
        _save.KeyEquivalent = "\r"; // Return saves, the way a Mac dialog's default button does
        _cancel = Button(content, "Cancel", Width - Padding - 100 - Gap - 100, 100, sameRow: true);
        _cancel.Activated += OnCancel;
        // U+001B is Escape, and claiming it here is what closes the window on it; a plain
        // NSWindow has no Escape handling. Escaped rather than the raw byte, which editors eat.
        _cancel.KeyEquivalent = "\u001b";

        // Closing must stop the meters, and the red button is a close this class would never hear
        // about otherwise: Dispose only runs when the shell next opens Settings, so a window
        // closed and left alone would keep a capture running for as long as the tray does.
        _window.WillClose += OnWillClose;

        // The cursor knows what the layout came to, so the document takes that height. At least
        // the viewport, so a short layout does not float the rows in a taller document.
        content.Frame = new CGRect(0, 0, Width, Math.Max(_y + Padding, Height));
    }

    // AppKit's origin is bottom-left, which would make a top-down cursor need the total height
    // before placing the first row. Flipped, y is a distance from the top and the layout runs.
    private sealed class FlippedView : NSView
    {
        public override bool IsFlipped => true;
    }

    // Named rather than lambdas so Dispose can unhook them: a handler that captures this
    // through a control AppKit retains is exactly what keeps a closed window alive.
    private void OnTest(object? sender, EventArgs e) => _ = TestConnectionAsync();

    private void OnTlsToggled(object? sender, EventArgs e) => ApplyTlsCoupling();

    // Live only under TLS, and cleared when TLS goes off so what the box says is what a Save
    // will keep.
    private void ApplyTlsCoupling()
    {
        bool tls = IsOn(_tls);
        _allowSelfSigned.Enabled = tls;
        if (!tls)
            _allowSelfSigned.State = NSCellStateValue.Off;
    }

    private void OnMicSensitivity(object? sender, EventArgs e) =>
        _micSensitivityReadout.StringValue = SettingsDraft.SensitivityLabel(_micSensitivity.IntValue);

    private void OnSystemSensitivity(object? sender, EventArgs e) =>
        _systemSensitivityReadout.StringValue = SettingsDraft.SensitivityLabel(_systemSensitivity.IntValue);

    private void OnSave(object? sender, EventArgs e) => Save();

    private void OnCancel(object? sender, EventArgs e) => Close();

    /// <summary>Release the window and everything it draws. ReleaseWhenClosed is off, which is
    /// what lets the shell ask IsOpen after a close, so without this the whole control graph
    /// survives every close, held by handlers that capture <c>this</c> through controls AppKit
    /// retains. Also stops an in-flight connection test posting into controls that are gone.
    /// </summary>
    public void Dispose()
    {
        if (_disposed)
            return;
        _disposed = true;
        _test.Activated -= OnTest;
        _tls.Activated -= OnTlsToggled;
        _micSensitivity.Activated -= OnMicSensitivity;
        _systemSensitivity.Activated -= OnSystemSensitivity;
        _micMeterOn.Activated -= OnMicMeterToggled;
        _systemMeterOn.Activated -= OnSystemMeterToggled;
        _save.Activated -= OnSave;
        _cancel.Activated -= OnCancel;
        _window.WillClose -= OnWillClose;
        StopMeters();
        _micProbe.Dispose();
        _systemProbe.Dispose();
        _window.Dispose();
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
        // End the edit first. A field being edited keeps its text in the window's field editor
        // and hands StringValue the LAST COMMITTED value, and clicking a button does not move
        // first responder off it. So typing a host and clicking Save with the mouse would save
        // the old one, silently. MakeFirstResponder(null) commits the text into the cell.
        _window.MakeFirstResponder(null);

        _draft.Host = _host.StringValue.Trim();
        _draft.Port = SettingsFields.Int(
            _port.StringValue, _draft.Port, min: SettingsBounds.PortMin, max: SettingsBounds.PortMax);
        _draft.Token = _token.StringValue;
        _draft.Tls = IsOn(_tls);
        _draft.AllowSelfSignedCert = IsOn(_allowSelfSigned);
        _draft.MicEnabled = IsOn(_micEnabled);
        _draft.MicName = _micName.StringValue;
        _draft.MicSensitivity = _micSensitivity.IntValue;
        _draft.SystemEnabled = IsOn(_systemEnabled);
        _draft.SystemName = _systemName.StringValue;
        _draft.SystemSensitivity = _systemSensitivity.IntValue;
        _draft.HangoverMs = SettingsFields.Int(
            _hangover.StringValue, _draft.HangoverMs, min: 0, max: SettingsBounds.HangoverMaxMs);
        _draft.PreRollMs = SettingsFields.Int(
            _preRoll.StringValue, _draft.PreRollMs, min: 0, max: SettingsBounds.PreRollMaxMs);
        _draft.ProcessOnEnd = IsOn(_processOnEnd);
        CollectPinGrid();
        return _draft.ToSettings();
    }

    // The same probe the SpatialChat bridge's popup runs: reachability, then a /tap handshake
    // under the reserved __probe__ identity that ConnectionTester owns. Collect() first, so
    // the test asks about what is typed rather than about what was last saved.
    private async Task TestConnectionAsync()
    {
        _test.Enabled = false;
        _testStatus.StringValue = "Testing…";

        string outcome;
        try
        {
            // Inside the try, not before it: the button is disabled and the status reads
            // "Testing…" from here, so anything that escapes leaves both stuck that way for as
            // long as the window is open.
            TapConnectionOptions options = Collect().ToConnectionOptions();
            using var timeout = new CancellationTokenSource(SettingsBounds.ConnectionTestTimeout);
            ConnectionTestResult result = await ConnectionTester
                .TestAsync(options, http: null, timeout.Token)
                .ConfigureAwait(false);
            outcome = result.Describe();
        }
        catch (Exception ex) when (ex is not OutOfMemoryException)
        {
            // Deliberately the widest filter here, like BridgeSettingsStore's token read.
            // ConnectionTester answers a bad host, a refused token and a timeout as RESULTS, so
            // what is left is a malformed entry throwing somewhere below. This runs
            // fire-and-forget from a click, so anything escaping is swallowed by the scheduler and
            // leaves the button dead with no answer at all. What is lost is the stack.
            outcome = $"Test failed: {ex.Message}";
        }

        // Back to the main thread before touching either control: the await above resumed on a
        // thread pool thread, because macOS has no SynchronizationContext to capture. Skipped
        // once the window is released, since the controls are gone by then.
        _dispatcher.Post(() =>
        {
            if (_disposed)
                return;
            _testStatus.StringValue = outcome;
            _test.Enabled = true;
        });
    }

    // ---- Layout ---------------------------------------------------------------------------
    // A top-down cursor rather than constraints: the window is fixed-size and single-column,
    // so the frames say what they mean and there is no layout pass to reason about.

    private static bool IsOn(NSButton check) => check.State == NSCellStateValue.On;

    // A toggle, a bar and a note on two rows. The bar sits under the sensitivity slider it
    // belongs to, so the marker and the slider read as one control.
    private (NSButton Toggle, LevelMeterView Bar, NSTextField Note) MeterRow(NSView content, string title)
    {
        nfloat y = NextRow(RowHeight);
        var toggle = new NSButton
        {
            Frame = new CGRect(ControlLeft, y, ControlWidth, RowHeight),
            Title = title,
        };
        toggle.SetButtonType(NSButtonType.Switch);
        content.AddSubview(toggle);

        nfloat barY = NextRow(14);
        var bar = new LevelMeterView(new CGRect(ControlLeft, barY, 240, 14));
        content.AddSubview(bar);

        return (toggle, bar, Note(content, "", lines: 1));
    }

    private void OnWillClose(object? sender, EventArgs e) => StopMeters();

    // Untick as well as stop, so reopening the window does not show two toggles claiming to be
    // on over two dead bars.
    private void StopMeters()
    {
        StopChannel(_micProbe, _micMeterOn, _micMeter);
        StopChannel(_systemProbe, _systemMeterOn, _systemMeter);
        StartOrStopMeterTimer();
    }

    private static void StopChannel(MeterProbe probe, NSButton toggle, LevelMeterView bar)
    {
        probe.Stop();
        toggle.State = NSCellStateValue.Off;
        bar.Level = 0;
    }

    private void OnMicMeterToggled(object? sender, EventArgs e) =>
        Toggle(_micProbe, _micMeterOn, _micMeter, _micMeterNote);

    private void OnSystemMeterToggled(object? sender, EventArgs e) =>
        Toggle(_systemProbe, _systemMeterOn, _systemMeter, _systemMeterNote);

    private void Toggle(MeterProbe probe, NSButton toggle, LevelMeterView bar, NSTextField note)
    {
        if (IsOn(toggle))
        {
            probe.Start();
            // Untick on a failure rather than leaving a toggle that claims to be on with a dead
            // bar under it. The note says why; the toggle says whether.
            if (!probe.Running)
                toggle.State = NSCellStateValue.Off;
        }
        else
        {
            probe.Stop();
            bar.Level = 0;
        }

        note.StringValue = probe.Error is { } why ? $"No level: {why}" : "";
        StartOrStopMeterTimer();
    }

    // One timer for both bars, running only while at least one is: an NSTimer on a settings
    // window that nobody is metering is a wakeup several times a second for nothing.
    private void StartOrStopMeterTimer()
    {
        bool wanted = _micProbe.Running || _systemProbe.Running;
        if (wanted == (_meterTimer is not null))
            return;

        if (!wanted)
        {
            _meterTimer!.Invalidate();
            _meterTimer = null;
            return;
        }

        // 15 Hz: the meter's own release smooths the bar, so a faster tick buys nothing an eye
        // can use and a slower one makes speech look like it arrives in steps.
        _meterTimer = NSTimer.CreateRepeatingScheduledTimer(1.0 / 15, _ => Tick());
    }

    private void Tick()
    {
        UpdateBar(_micMeter, _micProbe, _micSensitivity);
        UpdateBar(_systemMeter, _systemProbe, _systemSensitivity);
    }

    // Thresholds come from the slider's LIVE value, not from the draft: the whole point of the
    // marker is watching it move as the slider does, before any Save.
    private static void UpdateBar(LevelMeterView bar, MeterProbe probe, NSSlider sensitivity)
    {
        bar.Threshold = GateTuning.SliderToThreshold(sensitivity.IntValue);
        bar.Level = probe.Level;
    }

    private void BuildPinGrid(NSView content)
    {
        if (_draft.DeviceRows.Count == 0)
        {
            // The empty case is a REPORT, not a blank space. SettingsSeed swallows a CoreAudio
            // enumeration failure so a wrong host or a rejected token stays fixable, and this is
            // where the operator learns that is why the grid is empty. A saved pin is carried
            // forward untouched by a Save made from this state.
            Note(
                content,
                "No audio devices could be listed, so there is nothing to pin. Any device you "
                + "pinned before is kept.",
                lines: 2);
            return;
        }

        foreach (PinnedDeviceRow row in _draft.DeviceRows)
        {
            nfloat y = NextRow(RowHeight);

            var pin = new NSButton
            {
                Frame = new CGRect(Padding, y, LabelWidth + Gap, RowHeight),
                Title = "Pin",
            };
            pin.SetButtonType(NSButtonType.Switch);
            pin.State = row.Pinned ? NSCellStateValue.On : NSCellStateValue.Off;
            content.AddSubview(pin);

            var name = new NSTextField
            {
                Frame = new CGRect(ControlLeft, y, ControlWidth, RowHeight),
                StringValue = row.Name,
                PlaceholderString = row.DisplayLabel,
            };
            content.AddSubview(name);

            // The device's own label under the editable name: the name is the SPEAKER identity,
            // the label is which endpoint it is.
            Note(content, row.DisplayLabel, lines: 1);

            _pinRows.Add((row, pin, name));
        }
    }

    // Written back before ToSettings reads them, because DeviceRows IS what it collects: an
    // edit left in a control is an edit the Save silently drops.
    private void CollectPinGrid()
    {
        foreach ((PinnedDeviceRow row, NSButton pin, NSTextField name) in _pinRows)
        {
            row.Pinned = IsOn(pin);
            row.Name = name.StringValue;
        }
    }

    private nfloat NextRow(nfloat height)
    {
        _lastRowTop = _y;
        _y += height + Gap;
        return _lastRowTop;
    }

    private void Section(NSView content, string title)
    {
        _y += Gap; // a little air above each heading
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

    private static void Caption(NSView content, string text, nfloat y)
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
        // A switch does not shrink its title to fit: ControlWidth clipped the longest of these to
        // "…when the meeting e…". Take what the text needs, and when the control column cannot
        // hold it, start at the left margin instead: a checkbox has no caption beside it, so that
        // space is free, and one row out of alignment beats a sentence cut in half.
        check.SizeToFit();
        nfloat needed = check.Frame.Width;
        nfloat left = needed <= Width - ControlLeft - Padding ? ControlLeft : Padding;
        check.Frame = new CGRect(left, y, Math.Min(needed, Width - left - Padding), RowHeight);
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

    /// <summary>The two grants a meeting needs, and what can still be done about each.
    ///
    /// Here rather than left to the first Start because that is where the prompts land today,
    /// mid-meeting, and a dismissed one records a silent microphone with nothing anywhere saying
    /// so. What each state MEANS is Core's <see cref="PermissionRow"/>; this renders it.</summary>
    private void PermissionRows(NSView content)
    {
        Row(PermissionRow.For(MicrophoneTitle, MicrophoneState()), Privacy.Microphone);
        // macOS 14.4 gained the process tap and no API for its consent, so this row can only ever
        // report Unknown. Shown anyway: half the operators who meet the silent-far-end failure
        // are looking for exactly this row, and the button still reaches the place that fixes it.
        Row(PermissionRow.For(SystemAudioTitle, PermissionState.Unknown), Privacy.AudioCapture);

        void Row(PermissionRow row, string privacyPane)
        {
            // One line where one is enough: the detail runs from a short "Granted." to a
            // sentence, and a fixed two left a blank row above every button.
            string text = $"{row.Title}: {row.Detail}";
            Note(content, text, lines: text.Length > 64 ? 2 : 1);
            if (row.ActionLabel is not { } label)
                return;

            NSButton button = Button(content, label, ControlLeft, 190);
            button.Activated += (_, _) =>
            {
                if (row.Action == PermissionAction.Request)
                    AVCaptureDevice.RequestAccessForMediaType(AVAuthorizationMediaType.Audio, _ => { });
                else
                    NSWorkspace.SharedWorkspace.OpenUrl(new NSUrl(privacyPane));
            };
        }
    }

    // What macOS says about the microphone right now. Asking does NOT prompt: only
    // RequestAccessForMediaType does, and only from NotDetermined.
    private static PermissionState MicrophoneState() =>
        AVCaptureDevice.GetAuthorizationStatus(AVAuthorizationMediaType.Audio) switch
        {
            AVAuthorizationStatus.Authorized => PermissionState.Granted,
            AVAuthorizationStatus.NotDetermined => PermissionState.NotDetermined,
            // Restricted is a policy the operator cannot lift either, so it reads the same to them.
            AVAuthorizationStatus.Denied or AVAuthorizationStatus.Restricted => PermissionState.Denied,
            _ => PermissionState.Unknown,
        };

    // The system's own names for these, so the row matches what the operator will be looking at
    // once the button has sent them there.
    private const string MicrophoneTitle = "Microphone";
    private const string SystemAudioTitle = "System Audio Recording";

    /// <summary>Deep links into System Settings' privacy panes. An anchor macOS does not know
    /// falls back to the Privacy root rather than failing, so the worst case is one more click.
    /// </summary>
    private static class Privacy
    {
        private const string Pane = "x-apple.systempreferences:com.apple.preference.security?";

        internal const string Microphone = Pane + "Privacy_Microphone";

        // Named after kTCCServiceAudioCapture, the service this grant belongs to.
        internal const string AudioCapture = Pane + "Privacy_AudioCapture";
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
        // sameRow puts a second button beside the one just placed: a Cancel/Save pair is one row.
        nfloat y = sameRow ? _lastRowTop : NextRow(RowHeight + 6);
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
