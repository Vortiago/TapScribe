using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// A small window showing a finished meeting summary with a copy-to-clipboard
/// button (issue #107, PRD story 25). The text it shows is the already-computed
/// <see cref="PipelineView.SummaryText"/> from the tested Core mapper, so this is
/// pure presentation — no decisions live here.
/// </summary>
internal sealed class SummaryForm : Form
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

    public SummaryForm(PipelineView view)
    {
        ArgumentNullException.ThrowIfNull(view);

        Text = "TapScribe — meeting summary";
        Width = 540;
        Height = 440;
        StartPosition = FormStartPosition.CenterScreen;
        MinimizeBox = false;

        _text.Text = view.SummaryText ?? "";

        var caption = new Label
        {
            Dock = DockStyle.Top,
            AutoSize = false,
            Height = 26,
            Padding = new Padding(2, 4, 2, 4),
            Text = CaptionFor(view.Summary),
        };

        var copy = new Button { Text = "Copy", Width = 90, Height = 28 };
        copy.Click += (_, _) => Copy(_text.Text);
        var close = new Button { Text = "Close", Width = 90, Height = 28, DialogResult = DialogResult.OK };
        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            FlowDirection = FlowDirection.RightToLeft,
            AutoSize = true,
            Padding = new Padding(0, 6, 0, 0),
        };
        buttons.Controls.Add(close);
        buttons.Controls.Add(copy);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill,
            ColumnCount = 1,
            RowCount = 3,
            Padding = new Padding(10),
        };
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));     // caption
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100)); // summary text
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));     // buttons
        layout.Controls.Add(caption, 0, 0);
        layout.Controls.Add(_text, 0, 1);
        layout.Controls.Add(buttons, 0, 2);

        Controls.Add(layout);
        AcceptButton = close;
    }

    // Where the summary came from, if the Recorder told us — a quiet caption above the text.
    private static string CaptionFor(PipelineSummary? summary)
    {
        if (!string.IsNullOrWhiteSpace(summary?.Model))
            return $"Meeting summary · {summary.Model}";
        if (!string.IsNullOrWhiteSpace(summary?.Source))
            return $"Meeting summary · {summary.Source}";
        return "Meeting summary";
    }

    private static void Copy(string text)
    {
        // Clipboard.SetText throws on an empty string; nothing to copy then anyway.
        if (!string.IsNullOrEmpty(text))
            Clipboard.SetText(text);
    }
}
