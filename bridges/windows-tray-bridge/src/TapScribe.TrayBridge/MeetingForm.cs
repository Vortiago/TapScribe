using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// A small window for one meeting: it renders the tested Core <see cref="MeetingFormView"/>
/// projection of a <see cref="PipelineView"/> onto a caption + a read-only text box + a
/// Copy button. Pure presentation — no decisions live here; <see cref="Render"/> just
/// re-applies the projection as new poll views arrive.
///
/// Two callers (#107 + #168): the End-meeting flow opens it straight at the finished
/// summary (<c>new MeetingForm(); Render(doneView)</c>), and a Past-meetings re-open
/// (#168) shows it immediately in the Loading state and feeds it a
/// <see cref="MeetingController"/>'s emissions — Loading → progress → summary (or a
/// "no longer available" failure). It starts in the Loading state so an empty window is
/// never shown.
/// </summary>
internal sealed class MeetingForm : Form
{
    private readonly TextBox _text = new()
    {
        Multiline = true,
        ReadOnly = true,
        ScrollBars = ScrollBars.Vertical,
        Dock = DockStyle.Fill,
        BorderStyle = BorderStyle.FixedSingle,
        Font = new System.Drawing.Font("Segoe UI", 10f),
    };

    private readonly Label _caption = new()
    {
        Dock = DockStyle.Top,
        AutoSize = false,
        Height = 26,
        Padding = new Padding(2, 4, 2, 4),
    };

    private readonly Button _copy = new() { Text = "Copy", Width = 90, Height = 28 };

    public MeetingForm()
    {
        Width = 540;
        Height = 440;
        StartPosition = FormStartPosition.CenterScreen;
        MinimizeBox = false;

        _copy.Click += (_, _) => Copy(_text.Text);
        var close = new Button { Text = "Close", Width = 90, Height = 28, DialogResult = DialogResult.OK };
        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            FlowDirection = FlowDirection.RightToLeft,
            AutoSize = true,
            Padding = new Padding(0, 6, 0, 0),
        };
        buttons.Controls.Add(close);
        buttons.Controls.Add(_copy);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 3,
            Padding = new Padding(10),
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));     // caption
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); // body text
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));     // buttons
        layout.Controls.Add(_caption, 0, 0);
        layout.Controls.Add(_text, 0, 1);
        layout.Controls.Add(buttons, 0, 2);

        Controls.Add(layout);
        AcceptButton = close;

        Apply(MeetingFormView.For(null)); // start in the Loading state
    }

    /// <summary>Re-render from the latest poll view (or null for the pre-first-poll loading
    /// state). Pure projection through the tested Core <see cref="MeetingFormView"/>.</summary>
    public void Render(PipelineView? view) => Apply(MeetingFormView.For(view));

    private void Apply(MeetingFormView v)
    {
        Text = v.Title;
        _caption.Text = v.Caption;
        _text.Text = v.Body;
        _copy.Enabled = v.CanCopy;
    }

    private static void Copy(string text)
    {
        // Clipboard.SetText throws on an empty string; nothing to copy then anyway.
        if (!string.IsNullOrEmpty(text))
            Clipboard.SetText(text);
    }

    // --- Test seams (Windows E2E, InternalsVisibleTo TapScribe.TrayBridge.Tests) ----------
    // MeetingForm has no public surface, so these let the WinForms E2E assert that Render
    // wires a MeetingFormView onto the REAL controls. Methods (not properties) so the WinForms
    // WFO1000 designer-serialization analyzer never applies. Never used by the app.
    internal string CurrentBodyText() => _text.Text;
    internal string CurrentCaption() => _caption.Text;
    internal bool CopyEnabled() => _copy.Enabled;
}
