using System.Drawing;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge.Windows;

/// <summary>
/// The modal settings dialog, in four tabs (issue #106):
/// <list type="bullet">
/// <item><b>Connection</b> — Recorder host/port/TLS/allow-self-signed/identity/name/token + Test connection.</item>
/// <item><b>Devices</b> — which devices to tap: the two follow-default rows (mic + system
/// loopback) plus every concrete endpoint for pinning, each with an editable identity/name
/// and its own sensitivity slider (per-device tuning, mapped to a linear RMS threshold via
/// <see cref="GateTuning"/>; ADR-0007).</item>
/// <item><b>Level gate</b> — the shared bridge-side gate knobs that apply to every device:
/// hangover (silence-to-close) and pre-roll in ms. Sensitivity is per device on the
/// Devices tab.</item>
/// <item><b>Meeting</b> — whether End meeting auto-runs the strip/transcribe/summarize
/// pipeline (<c>ProcessOnEnd</c>), or just saves the session + recordings for later
/// dashboard processing.</item>
/// </list>
/// On Save it returns the edited <see cref="BridgeSettings"/> via <see cref="Result"/>;
/// the caller persists them. The device list is supplied by a delegate so the dialog
/// doesn't own enumerator lifecycle and a Refresh can re-enumerate.
///
/// <para><b>Nothing here is positioned by hand.</b> Every control sits in a
/// <see cref="TableLayoutPanel"/> row and is either <c>AutoSize</c> or docked, and the form
/// declares <see cref="ContainerControl.AutoScaleMode"/> <c>Font</c> against the 96-DPI
/// Segoe UI 9pt metrics the sizes here are written in — so the layout is a function of the
/// text the operator actually sees. The earlier version placed every control at a literal
/// pixel and gave each paragraph a literal height, which was correct only at 100% scale: on
/// a 150% display the font came back 1.6x taller while the constants did not, so the
/// descriptions were truncated to one line, captions sat on top of their inputs, "Test
/// connection" lost half its caption, and Save/Cancel fell off the bottom edge.</para>
/// </summary>
internal sealed class SettingsForm : Form
{
    private readonly Func<IReadOnlyList<CaptureDevice>> _listDevices;
    // All the editing logic lives in this pure, unit-tested view-model; the form is a thin
    // two-way binding of controls onto it (seeded on build, synced back on Save).
    private readonly SettingsDraft _draft;

    // Connection tab.
    private readonly TextBox _host = new() { Dock = DockStyle.Fill };
    private readonly NumericUpDown _port = Spinner(SettingsBounds.PortMin, SettingsBounds.PortMax);
    private readonly CheckBox _tls = new() { Text = "Use TLS (wss://)", AutoSize = true };
    // The full "accepts any cert / testing only" caveat lives in the README.
    private readonly CheckBox _allowSelfSigned = new()
    {
        Text = "Allow self-signed certificate (insecure)",
        AutoSize = true,
        // Indented under TLS to read as its sub-option; scaled with everything else.
        Margin = new Padding(24, 3, 3, 3),
    };
    private readonly TextBox _token = new() { UseSystemPasswordChar = true, Dock = DockStyle.Fill };
    private readonly CheckBox _showToken = new() { Text = "Show token", AutoSize = true };
    private readonly Button _testButton = TrayLayout.Action("Test connection");
    private readonly Label _testStatus = TrayLayout.Wrapped();

    // Devices tab — the common case is two checkboxes; pinning specific devices lives
    // behind the Advanced expander. One Name per device: it labels the source on the
    // dashboard AND (made filename-safe by the Recorder) tags it in the recordings.
    private readonly CheckBox _micEnabled = new() { Text = "Capture my microphone", AutoSize = true };
    private readonly TextBox _micName = new() { Dock = DockStyle.Fill };
    private readonly CheckBox _systemEnabled =
        new() { Text = "Capture system audio (the other side of the meeting)", AutoSize = true };
    private readonly TextBox _systemName = new() { Dock = DockStyle.Fill };
    private readonly LinkLabel _advancedToggle = new() { AutoSize = true, Margin = new Padding(3, 10, 3, 6) };
    private readonly TableLayoutPanel _advancedPanel = new() { Visible = false, ColumnCount = 1 };
    // Dock.Top over a height set in FitMeasuredSizes, not Dock.Fill in a stretching row: the
    // row it would stretch in is the one that takes what the others left, which at a large
    // text scale is nothing — and a Percent row cannot honour a child's MinimumSize, so the
    // grid would have overlapped the button under it rather than being given room.
    private readonly DataGridView _devices = new() { Dock = DockStyle.Top };
    private readonly Label _deviceStatus = TrayLayout.Wrapped();

    // Per-device sensitivity lives on the Devices tab — one slider per device — because a
    // mic and a system loopback want opposite sensitivity (ADR-0007). Hangover / pre-roll
    // are shared across devices and stay on the Level-gate tab.
    private readonly TrackBar _micSensitivity = Slider();
    private readonly Label _micSensitivityValue = new() { AutoSize = true };
    private readonly TrackBar _systemSensitivity = Slider();
    private readonly Label _systemSensitivityValue = new() { AutoSize = true };

    // Live per-device input-level meters (#152): a bar under each sensitivity slider, fed on a
    // UI-thread timer so the operator tunes against the level they see. Display only, never on
    // the tap/gate pipeline.
    private readonly LevelMeterBar _micMeter = Meter();
    private readonly LevelMeterBar _systemMeter = Meter();
    private readonly System.Windows.Forms.Timer _meterTimer = new() { Interval = 50 };
    private readonly MeterProbe _micProbe;
    private readonly MeterProbe _systemProbe;

    // Level-gate tab — the shared knobs.
    private readonly NumericUpDown _hangover = Spinner(0, SettingsBounds.HangoverMaxMs, 50);
    private readonly NumericUpDown _preRoll = Spinner(0, SettingsBounds.PreRollMaxMs, 50);

    // Meeting tab — what End meeting does. On: run the recorder's strip/transcribe/summarize
    // pipeline and show the summary. Off: just save the session + recordings for the dashboard.
    private readonly CheckBox _processOnEnd = new()
    {
        Text = "Transcribe and summarize automatically when the meeting ends",
        AutoSize = true,
    };

    public BridgeSettings Result { get; private set; }

    public SettingsForm(
        BridgeSettings current,
        Func<IReadOnlyList<CaptureDevice>> listDevices,
        Func<IAudioDeviceEnumerator> openEnumerator)
    {
        _listDevices = listDevices;
        _draft = SettingsDraft.Seed(current);
        _deviceStatus.ForeColor = Color.Firebrick;
        // CaptureDevice.DefaultFor, not a comparison written here: the meter must sample the
        // endpoint the gate it is tuning will tap, and re-deriving that rule is how they drift.
        _micProbe = new MeterProbe(
            openEnumerator, devices => CaptureDevice.DefaultFor(devices, DeviceFlow.Capture));
        _systemProbe = new MeterProbe(
            openEnumerator, devices => CaptureDevice.DefaultFor(devices, DeviceFlow.Render));
        Result = current;

        SuspendLayout(); // required by UseFontScaling below, which says why

        Text = "TapScribe — Settings";
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        TrayLayout.UseFontScaling(this);
        // Logical (96-DPI) units, scaled by the line above, and sized to the tallest tab
        // (Devices, with two meters and a status line) so the dialog opens unscrolled —
        // EveryTabFitsTheDialogItOpensAt fails if a new row outgrows it.
        ClientSize = new Size(560, 660);
        // Sizable rather than FixedDialog: every tab body scrolls, so nothing can become
        // unreachable, and an operator at a text scale nobody anticipated can widen the
        // dialog instead of losing the pin grid.
        FormBorderStyle = FormBorderStyle.Sizable;

        var tabs = new TabControl { Dock = DockStyle.Fill };
        tabs.TabPages.Add(BuildConnectionTab());
        tabs.TabPages.Add(BuildDevicesTab());
        tabs.TabPages.Add(BuildLevelGateTab());
        tabs.TabPages.Add(BuildMeetingTab());

        Button save = TrayLayout.Action("Save");
        save.DialogResult = DialogResult.OK;
        save.Click += (_, _) => Result = Collect();
        Button cancel = TrayLayout.Action("Cancel");
        cancel.DialogResult = DialogResult.Cancel;
        // RightToLeft flow, so Cancel is added first to land rightmost. The row AutoSizes,
        // which is what keeps both buttons on screen whatever the caption or the scale.
        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Fill,
            FlowDirection = FlowDirection.RightToLeft,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            Margin = new Padding(0, 8, 0, 0),
        };
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(save);

        var root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, Padding = new Padding(8) };
        root.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        Stack(root, tabs, fill: true);
        Stack(root, buttons);
        Controls.Add(root);

        AcceptButton = save;
        CancelButton = cancel;

        // The live meters run only while the dialog is open (#152): start once it's shown,
        // tear down the instant it closes — sampling stays off the UI thread (it runs on the
        // capture thread) and stops cleanly on close.
        Load += (_, _) => StartMeters();
        FormClosing += (_, _) => StopMeters();
        Load += (_, _) => FitToDesktop();
        FontChanged += (_, _) => FitMeasuredSizes();

        ResumeLayout(performLayout: true); // performs the auto-scale over the finished tree
        FitMeasuredSizes();                // measured against the font that scale settled on
    }

    // ---- Layout vocabulary -----------------------------------------------------------
    //
    // Four shapes here, three more in TrayLayout (which the meeting window shares).
    // They exist so that no tab reaches for a coordinate: a row is added, and its height
    // is whatever the text in it needs.

    /// <summary>A tab body: one full-width column of rows, which scrolls if the content
    /// outgrows the page. That scroll is the backstop that makes every size in this file
    /// safe to be wrong.</summary>
    private static TableLayoutPanel Page()
    {
        var page = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            AutoScroll = true,
            Padding = new Padding(10),
        };
        page.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        return page;
    }

    /// <summary>A caption/input grid: captions size to the longest one, inputs take the
    /// rest. GrowAndShrink because the default GrowOnly floors a fresh panel at its 200x100
    /// design size, which would leave a hole under every short grid.</summary>
    private static TableLayoutPanel Fields()
    {
        var grid = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            AutoSizeMode = AutoSizeMode.GrowAndShrink,
            ColumnCount = 2,
        };
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        return grid;
    }

    private static TrackBar Slider() =>
        new() { Minimum = 0, Maximum = 100, TickFrequency = 10, Dock = DockStyle.Top };

    /// <summary>
    /// A numeric spinner. Its width is set in <see cref="FitMeasuredSizes"/> rather than
    /// here, because a field initializer sees the default font, not the scaled one the
    /// operator reads.
    /// </summary>
    private static NumericUpDown Spinner(decimal min, decimal max, decimal step = 1) =>
        new() { Minimum = min, Maximum = max, Increment = step, Anchor = AnchorStyles.Left };

    /// <summary>
    /// The two sizes WinForms cannot derive on its own, measured once the auto-scale has
    /// settled the font — which is why this runs after ResumeLayout, and again on every font
    /// change. Everything else in the dialog sizes itself.
    /// </summary>
    private void FitMeasuredSizes()
    {
        // NumericUpDown is one of the few controls that ignores AutoSize, so someone has to
        // set its width — and a literal is how the port box came to render "800" for 8001 on
        // a scaled display. The spin buttons are as wide as a scrollbar's arrows, the one
        // platform metric that tracks them.
        foreach (NumericUpDown box in new[] { _port, _hangover, _preRoll })
        {
            box.Width = TextRenderer.MeasureText($"{box.Maximum}", box.Font).Width
                + SystemInformation.VerticalScrollBarWidth
                + box.Padding.Horizontal;
        }

        // The pin grid's height, which nothing else can answer: docked to the top of an
        // AutoSize row, this is what the row becomes, and the page scrolls to reach it. A
        // header plus four rows — enough to read as a list, with the grid's own scrollbar for
        // the rest, rather than the one-pixel grid a stretching row gave it at 150%.
        _devices.Height = _devices.ColumnHeadersHeight + (4 * _devices.RowTemplate.Height);
    }

    // Dock.Top over a docked height: a plain Control reports its own size as its preferred
    // one, so the row takes this height and the bar spans the column.
    private static LevelMeterBar Meter() => new() { Dock = DockStyle.Top, Height = 16 };

    /// <summary>
    /// One caption/input row in a <see cref="Fields"/> grid. A blank caption keeps the input
    /// in the second column, under the one above it.
    /// </summary>
    /// <param name="anchor">Where the caption sits in its cell. Left (centred vertically)
    /// reads best beside a one-line input; a tall input — the sensitivity slider, whose row
    /// is three times a caption's height because of its tick marks — wants Top, or the
    /// caption drifts down until it looks like it labels the row beneath.</param>
    private static void Field(
        TableLayoutPanel grid, string caption, Control input, AnchorStyles anchor = AnchorStyles.Left)
    {
        int row = grid.RowCount++;
        grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        grid.Controls.Add(new Label { Text = caption, AutoSize = true, Anchor = anchor }, 0, row);
        grid.Controls.Add(input, 1, row);
    }

    /// <summary>One full-width row in a <see cref="Fields"/> grid — a checkbox, which has no
    /// caption of its own.</summary>
    private static void Span(TableLayoutPanel grid, Control control)
    {
        int row = grid.RowCount++;
        grid.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        grid.Controls.Add(control, 0, row);
        grid.SetColumnSpan(control, 2);
    }

    /// <summary>Append one row to a single-column panel, sized to its content — or, with
    /// <paramref name="fill"/>, given every pixel the rest of the rows did not take.</summary>
    private static void Stack(TableLayoutPanel panel, Control control, bool fill = false)
    {
        int row = panel.RowCount++;
        panel.RowStyles.Add(fill ? new RowStyle(SizeType.Percent, 100) : new RowStyle(SizeType.AutoSize));
        panel.Controls.Add(control, 0, row);
    }

    // ---- Tabs ------------------------------------------------------------------------

    private TabPage BuildConnectionTab()
    {
        var page = new TabPage("Connection");
        TableLayoutPanel body = Page();

        TableLayoutPanel fields = Fields();
        Field(fields, "Recorder host", _host);
        _host.Text = _draft.Host;
        Field(fields, "Port", _port);
        _port.Value = Math.Clamp(_draft.Port, SettingsBounds.PortMin, SettingsBounds.PortMax);
        Span(fields, _tls);
        _tls.Checked = _draft.Tls;
        // Only meaningful over wss://, so it is greyed out unless TLS is on and forced off
        // when TLS is turned off — the same Tls && AllowSelfSignedCert scoping the connection
        // sites enforce, surfaced in the UI.
        Span(fields, _allowSelfSigned);
        _allowSelfSigned.Checked = _draft.Tls && _draft.AllowSelfSignedCert;
        _tls.CheckedChanged += (_, _) => SyncSelfSignedEnabled();
        SyncSelfSignedEnabled();
        Field(fields, "Tap token", _token);
        _token.Text = _draft.Token;
        Span(fields, _showToken);
        _showToken.CheckedChanged += (_, _) => _token.UseSystemPasswordChar = !_showToken.Checked;

        // Fire-and-forget (not async void): an unexpected fault can't crash the dialog.
        // ConnectionTester returns failures as a result, not exceptions.
        _testButton.Click += (_, _) => _ = TestConnectionAsync();

        Stack(body, fields);
        Stack(body, TrayLayout.Paragraph("Leave the token empty for a Recorder started with --no-auth."));
        Stack(body, _testButton);
        Stack(body, _testStatus);

        page.Controls.Add(body);
        return page;

        // "Allow self-signed" only applies over TLS: disable it without TLS and force it
        // off, so a saved value can never be collected while TLS is off.
        void SyncSelfSignedEnabled()
        {
            _allowSelfSigned.Enabled = _tls.Checked;
            if (!_tls.Checked)
                _allowSelfSigned.Checked = false;
        }
    }

    private TabPage BuildDevicesTab()
    {
        var page = new TabPage("Devices");
        TableLayoutPanel body = Page();

        // The common case: two checkboxes, each with an identity/name. "Follow default"
        // (these) tracks whatever the current default device is at Start; pinning a
        // specific endpoint lives behind the Advanced expander below. Control state is
        // seeded from the draft (which encodes the saved/migrated/default tuning).
        _micEnabled.Checked = _draft.MicEnabled;
        _micName.Text = _draft.MicName;
        _micSensitivity.Value = Math.Clamp(_draft.MicSensitivity, 0, 100);
        _systemEnabled.Checked = _draft.SystemEnabled;
        _systemName.Text = _draft.SystemName;
        _systemSensitivity.Value = Math.Clamp(_draft.SystemSensitivity, 0, 100);

        Stack(body, TrayLayout.Paragraph(
            "Name labels each source on the dashboard and tags it in the recording "
            + "filenames (made filename-safe automatically). Give the two different "
            + "names. Sensitivity is per device — open the loopback more than the mic."));
        Stack(body, Device(_micEnabled, _micName, _micSensitivity, _micSensitivityValue, _micMeter));
        Stack(body, Device(_systemEnabled, _systemName, _systemSensitivity, _systemSensitivityValue, _systemMeter));
        Stack(body, _deviceStatus);

        SetAdvancedToggle(open: false);
        _advancedToggle.LinkClicked += (_, _) =>
        {
            _advancedPanel.Visible = !_advancedPanel.Visible;
            SetAdvancedToggle(_advancedPanel.Visible);
        };
        Stack(body, _advancedToggle);

        _devices.AllowUserToAddRows = false;
        _devices.AllowUserToDeleteRows = false;
        _devices.RowHeadersVisible = false;
        _devices.SelectionMode = DataGridViewSelectionMode.CellSelect;
        // Fill, not literal widths: the grid divides whatever width the dialog has, so a
        // long endpoint name stays readable at any scale instead of clipping at 210px.
        _devices.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill;
        _devices.Columns.Add(new DataGridViewCheckBoxColumn { Name = "Tap", HeaderText = "Pin", FillWeight = 12 });
        _devices.Columns.Add(new DataGridViewTextBoxColumn
        {
            Name = "Device", HeaderText = "Device", FillWeight = 53, ReadOnly = true,
        });
        _devices.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "Name", FillWeight = 35 });
        // A checkbox edit commits immediately, so Collect() sees it without a focus change.
        _devices.CurrentCellDirtyStateChanged += (_, _) =>
        {
            if (_devices.IsCurrentCellDirty)
                _devices.CommitEdit(DataGridViewDataErrorContexts.Commit);
        };

        Button refresh = TrayLayout.Action("Refresh devices");
        // Re-enumerate the pin grid AND re-point the live meters at the now-current
        // follow-default endpoints (a just-plugged-in or newly-defaulted device).
        refresh.Click += (_, _) =>
        {
            PopulateDevices();
            RestartMeters();
        };

        _advancedPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        _advancedPanel.AutoSize = true;
        _advancedPanel.AutoSizeMode = AutoSizeMode.GrowAndShrink;
        _advancedPanel.Dock = DockStyle.Top;
        Stack(_advancedPanel, _devices);
        Stack(_advancedPanel, refresh);
        Stack(body, _advancedPanel);

        // Auto-open Advanced when a pinned device was saved, so it isn't hidden.
        if (_draft.HasSavedPins)
        {
            _advancedPanel.Visible = true;
            SetAdvancedToggle(open: true);
        }

        PopulateDevices();
        page.Controls.Add(body);
        return page;

        void SetAdvancedToggle(bool open) =>
            _advancedToggle.Text = (open ? "▾" : "▸") + " Advanced — pin specific devices…";
    }

    /// <summary>
    /// One device's block: enable, name, sensitivity slider, the slider's live
    /// RMS-threshold readout, and the level meter beneath it. Both devices are built from
    /// here, so the mic and the loopback cannot drift apart.
    ///
    /// The meter rides directly under the readout on the same RMS scale as the threshold it
    /// marks: the slider sets the marker (the level the input must clear to open the gate),
    /// and the UI timer pushes the live level into the bar.
    /// </summary>
    private static TableLayoutPanel Device(
        CheckBox enabled, TextBox name, TrackBar slider, Label readout, LevelMeterBar meter)
    {
        TableLayoutPanel block = Fields();
        block.Margin = new Padding(3, 3, 3, 12);
        Span(block, enabled);
        Field(block, "Name", name);
        Field(block, "Sensitivity", slider, AnchorStyles.Left | AnchorStyles.Top);
        Field(block, "", readout);
        Field(block, "", meter);

        meter.Threshold = GateTuning.SliderToThreshold(slider.Value);
        slider.ValueChanged += (_, _) =>
        {
            UpdateSensitivityLabel(slider, readout);
            meter.Threshold = GateTuning.SliderToThreshold(slider.Value);
        };
        UpdateSensitivityLabel(slider, readout);
        return block;
    }

    private TabPage BuildLevelGateTab()
    {
        var page = new TabPage("Level gate");
        TableLayoutPanel body = Page();

        Stack(body, TrayLayout.Paragraph(
            "The bridge opens a recording when the input level crosses the threshold "
            + "and closes it after the hangover. Sensitivity is set per device on the "
            + "Devices tab; hangover and pre-roll below apply to every device."));

        TableLayoutPanel fields = Fields();
        Field(fields, "Hangover (ms)", _hangover);
        _hangover.Value = Math.Clamp(_draft.HangoverMs, 0, SettingsBounds.HangoverMaxMs);
        Field(fields, "Pre-roll (ms)", _preRoll);
        _preRoll.Value = Math.Clamp(_draft.PreRollMs, 0, SettingsBounds.PreRollMaxMs);
        Stack(body, fields);

        page.Controls.Add(body);
        return page;
    }

    private TabPage BuildMeetingTab()
    {
        var page = new TabPage("Meeting");
        TableLayoutPanel body = Page();

        _processOnEnd.Checked = _draft.ProcessOnEnd;
        Stack(body, _processOnEnd);
        Stack(body, TrayLayout.Paragraph(
            "When on, End meeting runs the recorder's strip -> transcribe -> summarize "
            + "pipeline and pops up the finished summary. When off, End meeting only saves "
            + "the session and its recordings on the recorder — open them on the dashboard "
            + "to transcribe or summarize whenever you want."));

        page.Controls.Add(body);
        return page;
    }

    // Never larger than the desktop it opens on, and never shrinkable below that. The
    // scaled ClientSize is a guess at how much room the content wants, and a guess that is
    // too big is how Save ends up under the taskbar.
    private void FitToDesktop()
    {
        Rectangle work = Screen.FromControl(this).WorkingArea;
        Size = new Size(Math.Min(Width, work.Width), Math.Min(Height, work.Height));
        MinimumSize = Size;
        CenterToScreen();
    }

    private static void UpdateSensitivityLabel(TrackBar slider, Label valueLabel) =>
        valueLabel.Text = SettingsDraft.SensitivityLabel(slider.Value);


    private void PopulateDevices()
    {
        _devices.Rows.Clear();

        // The follow-default checkboxes resolve at Start, so a dialog that could not list
        // stays usable and simply shows no pin rows.
        DeviceListing listing = SettingsSeed.Listing(_listDevices);
        IReadOnlyList<CaptureDevice> available = listing.Devices;
        _deviceStatus.Text =
            listing.Error is { } why ? $"Could not list devices: {why}" : "";

        // The draft computes the pin rows (pre-ticked/named from saved pins) and the
        // absent-pin carry-forward; the form just renders the rows into the grid.
        _draft.SetAvailableDevices(available);
        foreach (PinnedDeviceRow deviceRow in _draft.DeviceRows)
        {
            int row = _devices.Rows.Add(deviceRow.Pinned, deviceRow.DisplayLabel, deviceRow.Name);
            _devices.Rows[row].Tag = deviceRow.DeviceId;
        }
    }

    // --- Live input-level meters (#152) ----------------------------------------------

    // Both meters on open, driven from a UI-thread timer. Best-effort: an absent or refused
    // device leaves its bar flat and says why rather than failing the dialog.
    private void StartMeters()
    {
        _micProbe.Start();
        _systemProbe.Start();
        ReportMeterErrors();
        if (!_micProbe.Running && !_systemProbe.Running)
            return;

        _meterTimer.Tick += OnMeterTick;
        _meterTimer.Start();
    }

    // A dead bar and a denied device look identical without this. Yields to an enumeration
    // failure, which causes both when it happens.
    private void ReportMeterErrors()
    {
        if (_deviceStatus.Text.Length > 0)
            return;

        (string Label, string? Error)[] meters =
            [("Microphone", _micProbe.Error), ("System audio", _systemProbe.Error)];
        _deviceStatus.Text = string.Join(
            "  ", meters.Where(m => m.Error is not null).Select(m => $"{m.Label} meter: {m.Error}"));
    }

    // Pull each meter's latest level (a torn-read-safe atomic updated on the capture thread)
    // into its bar. Cheap and non-blocking, which is what belongs on a UI timer tick.
    private void OnMeterTick(object? sender, EventArgs e)
    {
        _micMeter.Level = _micProbe.Level;
        _systemMeter.Level = _systemProbe.Level;
    }

    // Re-open the meters against the devices present now — wired to Refresh devices so that
    // switching the default mic/output (then refreshing) re-points the bars at the new
    // follow-default endpoints rather than the ones captured at open.
    private void RestartMeters()
    {
        StopMeters();
        StartMeters();
    }

    // Stop and release both meters and the timer. Idempotent: runs on FormClosing and again
    // from Dispose, and a second call is a no-op (the samplers are nulled out).
    private void StopMeters()
    {
        _meterTimer.Stop();
        _meterTimer.Tick -= OnMeterTick;
        _micProbe.Stop();
        _systemProbe.Stop();
        // Drop both bars to empty: if a Refresh loses a device, no meter reopens and the
        // timer won't tick, so without this the bar would stay frozen at its last level.
        _micMeter.Level = 0;
        _systemMeter.Level = 0;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            StopMeters(); // backstop for the FormClosing teardown; releases the captures
            _micProbe.Dispose();
            _systemProbe.Dispose();
            _meterTimer.Dispose();
        }
        base.Dispose(disposing);
    }

    private async Task TestConnectionAsync()
    {
        _testButton.Enabled = false;
        SetTestStatus("Testing…", SystemColors.GrayText);
        try
        {
            TapConnectionOptions options = Collect().ToConnectionOptions();
            // No ConfigureAwait(false): resume on the UI thread to update controls.
            ConnectionTestOutcome outcome = await ConnectionTester.DescribeAsync(options);
            SetTestStatus(outcome.Text, outcome.Ok ? Color.Green : Color.Firebrick);
        }
        catch (Exception ex) when (ex is not OutOfMemoryException)
        {
            // Collect throwing on a malformed entry. Fire-and-forget from a click, so an escapee
            // is swallowed by the scheduler and strands the status line on "Testing…" for as
            // long as the dialog is open. DescribeAsync guards the probe half.
            SetTestStatus($"Test failed: {ex.Message}", Color.Firebrick);
        }
        finally
        {
            _testButton.Enabled = true;
        }
    }

    private void SetTestStatus(string text, Color color)
    {
        _testStatus.ForeColor = color;
        _testStatus.Text = text;
    }

    // Collect the dialog's current control state into the draft, then let the (pure,
    // unit-tested) draft assemble the BridgeSettings — all the selection-/gate-building
    // decisions live there, not here.
    private BridgeSettings Collect()
    {
        _devices.EndEdit();
        SyncControlsToDraft();
        return _draft.ToSettings();
    }

    // Copy every editable control value (trimming is the draft's job) and the grid's
    // in-place pin/name edits back onto the draft before it builds the settings.
    private void SyncControlsToDraft()
    {
        _draft.Host = _host.Text;
        _draft.Port = (int)_port.Value;
        _draft.Tls = _tls.Checked;
        // The checkbox as ticked, unscoped: SettingsDraft.ToSettings gates the insecure opt-in
        // on TLS, so the pairing holds for both shells rather than for whichever dialog
        // remembers it. The UI still greys the box out and force-clears it on the TLS toggle
        // (BuildConnectionTab), which is the same rule surfaced rather than a second enforcer.
        _draft.AllowSelfSignedCert = _allowSelfSigned.Checked;
        _draft.Token = _token.Text;
        _draft.MicEnabled = _micEnabled.Checked;
        _draft.MicName = _micName.Text;
        _draft.MicSensitivity = _micSensitivity.Value;
        _draft.SystemEnabled = _systemEnabled.Checked;
        _draft.SystemName = _systemName.Text;
        _draft.SystemSensitivity = _systemSensitivity.Value;
        _draft.HangoverMs = (int)_hangover.Value;
        _draft.PreRollMs = (int)_preRoll.Value;
        _draft.ProcessOnEnd = _processOnEnd.Checked;

        // Indexer (not ToDictionary) so a duplicate device id from the injected enumerator
        // doesn't throw into the WinForms message loop — last row wins, as it did before.
        var rowsById = new Dictionary<string, PinnedDeviceRow>(StringComparer.Ordinal);
        foreach (PinnedDeviceRow draftRow in _draft.DeviceRows)
            rowsById[draftRow.DeviceId] = draftRow;
        foreach (DataGridViewRow row in _devices.Rows)
        {
            if (row.Tag is string deviceId && rowsById.TryGetValue(deviceId, out PinnedDeviceRow? draftRow))
            {
                draftRow.Pinned = row.Cells["Tap"].Value is true;
                draftRow.Name = row.Cells["Name"].Value as string ?? "";
            }
        }
    }
}
