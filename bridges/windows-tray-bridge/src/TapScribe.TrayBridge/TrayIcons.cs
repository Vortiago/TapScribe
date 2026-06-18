using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// The three at-a-glance tray icons (idle / streaming / error), drawn at runtime as a
/// coloured dot rather than shipped as binary <c>.ico</c> assets. <see cref="StatusView"/>
/// chooses which <see cref="TrayIcon"/> to show; this maps that to a concrete icon. Built
/// once and reused for the process lifetime; <see cref="Dispose"/> releases the GDI handles.
/// </summary>
internal sealed class TrayIcons : IDisposable
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr handle);

    private readonly Dictionary<TrayIcon, Icon> _icons = new()
    {
        [TrayIcon.Idle] = Dot(Color.Gray),
        [TrayIcon.Streaming] = Dot(Color.LimeGreen),
        [TrayIcon.Error] = Dot(Color.Firebrick),
    };

    public Icon this[TrayIcon key] => _icons[key];

    private static Icon Dot(Color color)
    {
        using var bitmap = new Bitmap(16, 16);
        using (Graphics g = Graphics.FromImage(bitmap))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);
            using var brush = new SolidBrush(color);
            g.FillEllipse(brush, 2, 2, 12, 12);
        }

        // Icon.FromHandle does not own the HICON, so capture it, build a standalone
        // Icon via its serialized form, and free the handle immediately — no per-icon
        // GDI leak for the life of the process.
        IntPtr handle = bitmap.GetHicon();
        try
        {
            using var temp = Icon.FromHandle(handle);
            return (Icon)temp.Clone();
        }
        finally
        {
            DestroyIcon(handle);
        }
    }

    public void Dispose()
    {
        foreach (Icon icon in _icons.Values)
            icon.Dispose();
        _icons.Clear();
    }
}
