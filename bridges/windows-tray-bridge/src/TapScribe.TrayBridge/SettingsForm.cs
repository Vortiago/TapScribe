using System.Drawing;
using TapScribe.Bridge.Core;
using TapScribe.Bridge.Windows;

namespace TapScribe.TrayBridge;

/// <summary>
/// A small modal dialog with input fields for the Recorder connection settings,
/// so the operator never has to set environment variables. A "Test connection"
/// button probes the Recorder (reachability + tap token) the way the SpatialChat
/// bridge does. On Save it returns the edited <see cref="BridgeSettings"/> via
/// <see cref="Result"/>; the caller persists them.
/// </summary>
internal sealed class SettingsForm : Form
{
    private readonly TextBox _host = new();
    private readonly NumericUpDown _port = new() { Minimum = 1, Maximum = 65535, Width = 90 };
    private readonly CheckBox _tls = new() { Text = "Use TLS (wss://)", AutoSize = true };
    private readonly TextBox _identity = new();
    private readonly TextBox _name = new();
    private readonly TextBox _token = new() { UseSystemPasswordChar = true };
    private readonly CheckBox _showToken = new() { Text = "Show token", AutoSize = true };
    private readonly Button _testButton = new() { Text = "Test connection", Width = 120 };
    private readonly Label _testStatus = new();

    public BridgeSettings Result { get; private set; }

    public SettingsForm(BridgeSettings current)
    {
        Result = current;

        Text = "TapScribe — Settings";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(400, 344);

        const int labelX = 12;
        const int inputX = 110;
        const int inputWidth = 276;
        int y = 16;

        _host.Text = current.Host;
        AddRow("Recorder host", _host, ref y);
        AddRow("Port", _port, ref y);
        _port.Value = Math.Clamp(current.Port, 1, 65535);
        AddCheck(_tls, current.Tls, ref y);
        _identity.Text = current.Identity;
        AddRow("Identity", _identity, ref y);
        _name.Text = current.Name;
        AddRow("Name", _name, ref y);
        _token.Text = current.Token;
        AddRow("Tap token", _token, ref y);
        AddCheck(_showToken, isChecked: false, ref y);
        _showToken.CheckedChanged += (_, _) => _token.UseSystemPasswordChar = !_showToken.Checked;

        var hint = new Label
        {
            Text = "Leave the token empty for a Recorder started with --no-auth.",
            Location = new Point(labelX, y + 4),
            AutoSize = true,
            ForeColor = SystemColors.GrayText,
        };
        Controls.Add(hint);

        _testStatus.Location = new Point(labelX, y + 28);
        _testStatus.Size = new Size(ClientSize.Width - 24, 40);
        Controls.Add(_testStatus);

        // Fire-and-forget (not async void): an unexpected fault can't crash the
        // dialog. ConnectionTester returns failures as a result, not exceptions.
        _testButton.Location = new Point(labelX, ClientSize.Height - 40);
        _testButton.Click += (_, _) => _ = TestConnectionAsync();
        Controls.Add(_testButton);

        var save = new Button { Text = "Save", DialogResult = DialogResult.OK, Width = 80 };
        var cancel = new Button { Text = "Cancel", DialogResult = DialogResult.Cancel, Width = 80 };
        save.Location = new Point(ClientSize.Width - 2 * 80 - 20, ClientSize.Height - 40);
        cancel.Location = new Point(ClientSize.Width - 80 - 12, ClientSize.Height - 40);
        save.Click += (_, _) => Result = Collect();
        Controls.Add(save);
        Controls.Add(cancel);
        AcceptButton = save;
        CancelButton = cancel;

        void AddRow(string label, Control input, ref int rowY)
        {
            Controls.Add(new Label { Text = label, Location = new Point(labelX, rowY + 3), AutoSize = true });
            input.Location = new Point(inputX, rowY);
            if (input is TextBox)
                input.Width = inputWidth;
            Controls.Add(input);
            rowY += 30;
        }

        void AddCheck(CheckBox check, bool isChecked, ref int rowY)
        {
            check.Checked = isChecked;
            check.Location = new Point(inputX, rowY + 2);
            Controls.Add(check);
            rowY += 28;
        }
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
            ConnectionTestResult result = await ConnectionTester.TestAsync(options, http: null, cts.Token);
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

    private BridgeSettings Collect() => new()
    {
        Host = _host.Text.Trim(),
        Port = (int)_port.Value,
        Tls = _tls.Checked,
        Identity = _identity.Text.Trim(),
        Name = _name.Text.Trim(),
        Token = _token.Text.Trim(),
    };
}
