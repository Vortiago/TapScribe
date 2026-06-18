using System.Drawing;
using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// The modal settings dialog, in three tabs (issue #106):
/// <list type="bullet">
/// <item><b>Connection</b> — Recorder host/port/TLS/identity/name/token + Test connection.</item>
/// <item><b>Devices</b> — which devices to tap: the two follow-default rows (mic + system
/// loopback) plus every concrete endpoint for pinning, each with an editable identity/name.</item>
/// <item><b>Level gate</b> — the bridge-side gate knobs: a sensitivity slider (mapped to a
/// linear RMS threshold via <see cref="GateTuning"/>) and hangover / pre-roll in ms.</item>
/// </list>
/// On Save it returns the edited <see cref="BridgeSettings"/> via <see cref="Result"/>;
/// the caller persists them. The device list is supplied by a delegate so the dialog
/// doesn't own enumerator lifecycle and a Refresh can re-enumerate.
/// </summary>
internal sealed class SettingsForm : Form
{
    private readonly Func<IReadOnlyList<CaptureDevice>> _listDevices;
    private readonly BridgeSettings _current;
    private readonly int _contentW;
    private readonly int _contentH;

    // Connection tab.
    private readonly TextBox _host = new();
    private readonly NumericUpDown _port = new() { Minimum = 1, Maximum = 65535, Width = 90 };
    private readonly CheckBox _tls = new() { Text = "Use TLS (wss://)", AutoSize = true };
    private readonly TextBox _token = new() { UseSystemPasswordChar = true };
    private readonly CheckBox _showToken = new() { Text = "Show token", AutoSize = true };
    private readonly Button _testButton = new() { Text = "Test connection", Width = 120 };
    private readonly Label _testStatus = new();

    // Devices tab — the common case is two checkboxes; pinning specific devices lives
    // behind the Advanced expander. One Name per device: it labels the source on the
    // dashboard AND (made filename-safe by the Recorder) tags it in the recordings.
    private readonly CheckBox _micEnabled = new() { Text = "Capture my microphone", AutoSize = true };
    private readonly TextBox _micName = new() { Width = 220 };
    private readonly CheckBox _systemEnabled =
        new() { Text = "Capture system audio (the other side of the meeting)", AutoSize = true };
    private readonly TextBox _systemName = new() { Width = 220 };
    private readonly LinkLabel _advancedToggle = new() { AutoSize = true };
    private readonly Panel _advancedPanel = new() { Visible = false };
    private readonly DataGridView _devices = new();
    private readonly Label _deviceStatus = new() { AutoSize = true, ForeColor = Color.Firebrick };

    // Pinned selections whose device isn't present right now, so they have no grid row.
    // Carried forward verbatim on Save so an unplugged-device pin isn't silently erased.
    private List<DeviceSelection> _absentPinned = [];

    // Level-gate tab.
    private readonly TrackBar _sensitivity = new() { Minimum = 0, Maximum = 100, TickFrequency = 10, Width = 300 };
    private readonly Label _sensitivityValue = new() { AutoSize = true };
    private readonly NumericUpDown _hangover = new() { Minimum = 0, Maximum = 5000, Increment = 50, Width = 90 };
    private readonly NumericUpDown _preRoll = new() { Minimum = 0, Maximum = 2000, Increment = 50, Width = 90 };

    public BridgeSettings Result { get; private set; }

    public SettingsForm(BridgeSettings current, Func<IReadOnlyList<CaptureDevice>> listDevices)
    {
        _current = current;
        _listDevices = listDevices;
        Result = current;

        Text = "TapScribe — Settings";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(460, 460);

        var tabs = new TabControl
        {
            Location = new Point(8, 8),
            Size = new Size(ClientSize.Width - 16, ClientSize.Height - 56),
        };
        // A TabPage doesn't get its real size until it's added to the TabControl and
        // laid out, so the Build*Tab methods can't trust page.Width/Height. Derive the
        // content area from the (known) TabControl size: minus the side borders and the
        // top tab strip. The dialog is FixedDialog, so these stay correct.
        _contentW = tabs.Width - 8;
        _contentH = tabs.Height - 28;
        tabs.TabPages.Add(BuildConnectionTab());
        tabs.TabPages.Add(BuildDevicesTab());
        tabs.TabPages.Add(BuildLevelGateTab());
        Controls.Add(tabs);

        var save = new Button { Text = "Save", DialogResult = DialogResult.OK, Width = 80 };
        var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Width = 80 };
        save.Location = new Point(ClientSize.Width - 2 * 80 - 20, ClientSize.Height - 38);
        cancel.Location = new Point(ClientSize.Width - 80 - 12, ClientSize.Height - 38);
        save.Click += (_, _) => Result = Collect();
        Controls.Add(save);
        Controls.Add(cancel);
        AcceptButton = save;
        CancelButton = cancel;
    }

    private TabPage BuildConnectionTab()
    {
        var page = new TabPage("Connection");
        const int labelX = 12;
        const int inputX = 110;
        const int inputWidth = 286;
        int y = 12;

        AddRow(page, "Recorder host", _host, ref y, inputWidth);
        _host.Text = _current.Host;
        AddRow(page, "Port", _port, ref y, inputWidth);
        _port.Value = Math.Clamp(_current.Port, 1, 65535);
        AddCheck(page, _tls, _current.Tls, ref y, inputX);
        AddRow(page, "Tap token", _token, ref y, inputWidth);
        _token.Text = _current.Token;
        AddCheck(page, _showToken, isChecked: false, ref y, inputX);
        _showToken.CheckedChanged += (_, _) => _token.UseSystemPasswordChar = !_showToken.Checked;

        page.Controls.Add(new Label
        {
            Text = "Leave the token empty for a Recorder started with --no-auth.",
            Location = new Point(labelX, y + 2),
            AutoSize = true,
            ForeColor = SystemColors.GrayText,
        });

        _testStatus.Location = new Point(labelX, y + 26);
        _testStatus.Size = new Size(_contentW - 24, 48);
        page.Controls.Add(_testStatus);

        // Fire-and-forget (not async void): an unexpected fault can't crash the dialog.
        // ConnectionTester returns failures as a result, not exceptions.
        _testButton.Location = new Point(labelX, _contentH - 44);
        _testButton.Click += (_, _) => _ = TestConnectionAsync();
        page.Controls.Add(_testButton);
        return page;

        static void AddRow(TabPage host, string label, Control input, ref int rowY, int width)
        {
            host.Controls.Add(new Label { Text = label, Location = new Point(12, rowY + 3), AutoSize = true });
            input.Location = new Point(110, rowY);
            if (input is TextBox)
                input.Width = width;
            host.Controls.Add(input);
            rowY += 30;
        }

        static void AddCheck(TabPage host, CheckBox check, bool isChecked, ref int rowY, int x)
        {
            check.Checked = isChecked;
            check.Location = new Point(x, rowY + 2);
            host.Controls.Add(check);
            rowY += 28;
        }
    }

    private TabPage BuildDevicesTab()
    {
        var page = new TabPage("Devices");

        // The common case: two checkboxes, each with an identity/name. "Follow default"
        // (these) tracks whatever the current default device is at Start; pinning a
        // specific endpoint lives behind the Advanced expander below.
        SeedSimpleSelections();

        page.Controls.Add(new Label
        {
            Text = "Name labels each source on the dashboard and tags it in the recording "
                 + "filenames (made filename-safe automatically). Give the two different names.",
            Location = new Point(12, 8),
            Size = new Size(_contentW - 24, 30),
            ForeColor = SystemColors.GrayText,
        });

        _micEnabled.Location = new Point(12, 44);
        page.Controls.Add(_micEnabled);
        AddNameRow(page, _micName, 70);

        _systemEnabled.Location = new Point(12, 108);
        page.Controls.Add(_systemEnabled);
        AddNameRow(page, _systemName, 134);

        _deviceStatus.Location = new Point(12, 168);
        _deviceStatus.MaximumSize = new Size(_contentW - 24, 0);
        page.Controls.Add(_deviceStatus);

        SetAdvancedToggle(open: false);
        _advancedToggle.Location = new Point(12, 190);
        _advancedToggle.LinkClicked += (_, _) =>
        {
            _advancedPanel.Visible = !_advancedPanel.Visible;
            SetAdvancedToggle(_advancedPanel.Visible);
        };
        page.Controls.Add(_advancedToggle);

        int panelW = _contentW - 24;
        int panelH = _contentH - 226;
        _advancedPanel.Location = new Point(12, 216);
        _advancedPanel.Size = new Size(panelW, panelH);
        _advancedPanel.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;

        _devices.Location = new Point(0, 0);
        _devices.Size = new Size(panelW, panelH - 32);
        _devices.Anchor = AnchorStyles.Top | AnchorStyles.Bottom | AnchorStyles.Left | AnchorStyles.Right;
        _devices.AllowUserToAddRows = false;
        _devices.AllowUserToDeleteRows = false;
        _devices.RowHeadersVisible = false;
        _devices.SelectionMode = DataGridViewSelectionMode.CellSelect;
        _devices.AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.None;
        _devices.Columns.Add(new DataGridViewCheckBoxColumn { Name = "Tap", HeaderText = "Pin", Width = 36 });
        _devices.Columns.Add(new DataGridViewTextBoxColumn
        {
            Name = "Device", HeaderText = "Device", Width = 210, ReadOnly = true,
        });
        _devices.Columns.Add(new DataGridViewTextBoxColumn { Name = "Name", HeaderText = "Name", Width = 150 });
        // A checkbox edit commits immediately, so Collect() sees it without a focus change.
        _devices.CurrentCellDirtyStateChanged += (_, _) =>
        {
            if (_devices.IsCurrentCellDirty)
                _devices.CommitEdit(DataGridViewDataErrorContexts.Commit);
        };
        _advancedPanel.Controls.Add(_devices);

        var refresh = new Button { Text = "Refresh devices", Width = 120, Location = new Point(0, panelH - 28) };
        refresh.Anchor = AnchorStyles.Bottom | AnchorStyles.Left;
        refresh.Click += (_, _) => PopulateDevices();
        _advancedPanel.Controls.Add(refresh);
        page.Controls.Add(_advancedPanel);

        // Auto-open Advanced when a pinned device was saved, so it isn't hidden.
        if (_current.Devices.Any(d => d is DeviceSelection.Pinned))
        {
            _advancedPanel.Visible = true;
            SetAdvancedToggle(open: true);
        }

        PopulateDevices();
        return page;

        void SetAdvancedToggle(bool open) =>
            _advancedToggle.Text = (open ? "▾" : "▸") + " Advanced — pin specific devices…";

        static void AddNameRow(TabPage host, TextBox name, int rowY)
        {
            host.Controls.Add(new Label { Text = "Name", Location = new Point(32, rowY + 3), AutoSize = true });
            name.Location = new Point(80, rowY);
            host.Controls.Add(name);
        }
    }

    // The single per-device label: the saved Name, or its identity for a legacy/blank Name.
    private static string SelectionLabel(DeviceSelection selection) =>
        string.IsNullOrWhiteSpace(selection.Name) ? selection.Identity : selection.Name;

    /// <summary>Seed the two simple checkboxes + their identity/name from the saved
    /// selection (or the default pair when nothing is saved — same as EffectiveDevices).</summary>
    private void SeedSimpleSelections()
    {
        // Sensible fallbacks from the shared default pair, so the fields are never blank
        // even when a box starts unticked (e.g. a pinned-only saved selection).
        foreach (DeviceSelection selection in _current.DefaultDevices())
            ApplyToSimpleRow(selection, tick: false);

        // Reflect what's actually saved — this ticks the boxes for saved follow-defaults.
        foreach (DeviceSelection selection in _current.EffectiveDevices)
            ApplyToSimpleRow(selection, tick: true);

        void ApplyToSimpleRow(DeviceSelection selection, bool tick)
        {
            if (selection is DeviceSelection.FollowDefault { Flow: DeviceFlow.Capture } mic)
            {
                _micEnabled.Checked |= tick;
                _micName.Text = SelectionLabel(mic);
            }
            else if (selection is DeviceSelection.FollowDefault { Flow: DeviceFlow.Render } system)
            {
                _systemEnabled.Checked |= tick;
                _systemName.Text = SelectionLabel(system);
            }
        }
    }

    private TabPage BuildLevelGateTab()
    {
        var page = new TabPage("Level gate");
        int y = 16;

        page.Controls.Add(new Label
        {
            Text = "The bridge opens a recording when the input level crosses the threshold "
                 + "and closes it after the hangover. Higher sensitivity opens on quieter sound.",
            Location = new Point(12, y),
            Size = new Size(_contentW - 24, 34),
            ForeColor = SystemColors.GrayText,
        });
        y += 44;

        page.Controls.Add(new Label { Text = "Sensitivity", Location = new Point(12, y + 4), AutoSize = true });
        _sensitivity.Location = new Point(110, y);
        _sensitivity.Value = Math.Clamp(_current.GateSensitivity, 0, 100);
        _sensitivity.ValueChanged += (_, _) => UpdateSensitivityLabel();
        page.Controls.Add(_sensitivity);
        _sensitivityValue.Location = new Point(110, y + 44);
        page.Controls.Add(_sensitivityValue);
        UpdateSensitivityLabel();
        y += 80;

        page.Controls.Add(new Label { Text = "Hangover (ms)", Location = new Point(12, y + 3), AutoSize = true });
        _hangover.Location = new Point(110, y);
        _hangover.Value = Math.Clamp(_current.GateHangoverMs, 0, 5000);
        page.Controls.Add(_hangover);
        y += 32;

        page.Controls.Add(new Label { Text = "Pre-roll (ms)", Location = new Point(12, y + 3), AutoSize = true });
        _preRoll.Location = new Point(110, y);
        _preRoll.Value = Math.Clamp(_current.GatePreRollMs, 0, 2000);
        page.Controls.Add(_preRoll);

        return page;
    }

    private void UpdateSensitivityLabel()
    {
        double threshold = GateTuning.SliderToThreshold(_sensitivity.Value);
        _sensitivityValue.Text = $"{_sensitivity.Value} / 100   (RMS threshold ≈ {threshold:0.000})";
    }

    private void PopulateDevices()
    {
        _devices.Rows.Clear();

        IReadOnlyList<CaptureDevice> available;
        try
        {
            available = _listDevices();
            _deviceStatus.Text = "";
        }
        catch (Exception ex) when (ex is COMException or InvalidOperationException)
        {
            // Enumeration failed (no audio service, COM error): the two follow-default
            // checkboxes still work (they resolve at Start), so just show no pin rows.
            available = [];
            _deviceStatus.Text = $"Could not list devices: {ex.Message}";
        }

        // Pre-tick any device the saved selection pinned.
        var savedPinned = new Dictionary<string, DeviceSelection.Pinned>(StringComparer.Ordinal);
        foreach (DeviceSelection.Pinned pinned in _current.Devices.OfType<DeviceSelection.Pinned>())
            savedPinned[pinned.DeviceId] = pinned;

        foreach (CaptureDevice device in available)
        {
            string flowLabel = device.Flow == DeviceFlow.Capture ? "mic" : "loopback";
            savedPinned.TryGetValue(device.Id, out DeviceSelection.Pinned? pinned);
            int row = _devices.Rows.Add(
                pinned is not null,
                $"{device.Name} [{flowLabel}{(device.IsDefault ? ", default" : "")}]",
                pinned is not null ? SelectionLabel(pinned) : device.Name);
            _devices.Rows[row].Tag = device.Id;
        }

        // A pinned device that isn't present right now has no row and so can't be
        // collected from the grid; remember it to carry forward verbatim on Save rather
        // than silently erasing the pin (the device may just be unplugged).
        var presentIds = new HashSet<string>(available.Select(d => d.Id), StringComparer.Ordinal);
        _absentPinned = _current.Devices
            .Where(d => d is DeviceSelection.Pinned p && !presentIds.Contains(p.DeviceId))
            .ToList();
    }

    private async Task TestConnectionAsync()
    {
        _testButton.Enabled = false;
        SetTestStatus("Testing…", SystemColors.GrayText);
        try
        {
            TapConnectionOptions options = Collect().ToConnectionOptions();
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            // No ConfigureAwait(false): resume on the UI thread to update controls.
            ConnectionTestResult result =
                await ConnectionTester.TestAsync(options, http: null, cts.Token);
            SetTestStatus(result.Describe(), result.Ok ? Color.Green : Color.Firebrick);
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

    private BridgeSettings Collect()
    {
        _devices.EndEdit();

        // One Name per device, used as both the identity (the Recorder makes it
        // filename-safe) and the display name.
        var selections = new List<DeviceSelection>();
        if (_micEnabled.Checked)
        {
            string mic = _micName.Text.Trim();
            selections.Add(new DeviceSelection.FollowDefault(DeviceFlow.Capture, mic, mic));
        }
        if (_systemEnabled.Checked)
        {
            string system = _systemName.Text.Trim();
            selections.Add(new DeviceSelection.FollowDefault(DeviceFlow.Render, system, system));
        }

        foreach (DataGridViewRow row in _devices.Rows)
        {
            if (row.Tag is not string deviceId || row.Cells["Tap"].Value is not true)
                continue;
            string name = (row.Cells["Name"].Value as string ?? "").Trim();
            selections.Add(new DeviceSelection.Pinned(deviceId, name, name));
        }

        // Keep pins whose device is currently absent (no row to collect from).
        selections.AddRange(_absentPinned);

        return new BridgeSettings
        {
            Host = _host.Text.Trim(),
            Port = (int)_port.Value,
            Tls = _tls.Checked,
            // The base identity/name are the env-seed / first-run default (they feed
            // DefaultDevices when nothing is saved); the authoritative per-tap identity
            // lives in Devices, so pass these through unchanged rather than re-deriving.
            Identity = _current.Identity,
            Name = _current.Name,
            Token = _token.Text.Trim(),
            Devices = selections,
            GateSensitivity = _sensitivity.Value,
            GateHangoverMs = (int)_hangover.Value,
            GatePreRollMs = (int)_preRoll.Value,
        };
    }
}
