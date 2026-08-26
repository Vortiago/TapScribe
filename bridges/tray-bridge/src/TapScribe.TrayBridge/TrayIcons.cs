using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using TapScribe.Bridge.Core;

namespace TapScribe.TrayBridge;

/// <summary>
/// The at-a-glance tray icons (idle / streaming / degraded / error), drawn at runtime as a
/// coloured dot rather than shipped as binary <c>.ico</c> assets. <see cref="StatusView"/>
/// chooses which <see cref="TrayIcon"/> to show; this maps that to a concrete icon. Built
/// once and reused for the process lifetime; <see cref="Dispose"/> releases the GDI handles.
/// </summary>
internal sealed class TrayIcons : IDisposable
{
    private readonly Dictionary<TrayIcon, Icon> _icons = new()
    {
        [TrayIcon.Idle] = Dot(Color.Gray),
        [TrayIcon.Streaming] = Dot(Color.LimeGreen),
        [TrayIcon.Degraded] = Dot(Color.Goldenrod),
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

        // Wrap the 16x16 PNG in a one-entry ICONDIR and hand it to the managed
        // Icon(Stream) constructor, which reads the data into its own copy. This avoids
        // bitmap.GetHicon() — whose HICON would have to be released via the user32
        // DestroyIcon P/Invoke — so there's no unmanaged handle to track and no GDI leak.
        using var png = new MemoryStream();
        bitmap.Save(png, ImageFormat.Png);
        byte[] pngBytes = png.ToArray();

        using var ico = new MemoryStream();
        using (var w = new BinaryWriter(ico, System.Text.Encoding.UTF8, leaveOpen: true))
        {
            // ICONDIR header.
            w.Write((short)0);            // reserved, must be 0
            w.Write((short)1);            // image type, 1 = icon
            w.Write((short)1);            // number of images
            // ICONDIRENTRY (PNG-encoded, supported by modern Windows icon loading).
            w.Write((byte)16);            // width
            w.Write((byte)16);            // height
            w.Write((byte)0);             // colours in palette (0 = no palette)
            w.Write((byte)0);             // reserved, must be 0
            w.Write((short)1);            // colour planes
            w.Write((short)32);           // bits per pixel
            w.Write(pngBytes.Length);     // size of the image data
            w.Write(6 + 16);              // offset of the image data from the start
            w.Write(pngBytes);
        }

        ico.Position = 0;
        return new Icon(ico, new Size(16, 16));
    }

    public void Dispose()
    {
        foreach (Icon icon in _icons.Values)
            icon.Dispose();
        _icons.Clear();
    }
}
