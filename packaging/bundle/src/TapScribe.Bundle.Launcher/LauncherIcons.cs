using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using TapScribe.Bundle.Core;

namespace TapScribe.Bundle.Launcher;

/// <summary>
/// The three at-a-glance tray icons, drawn at runtime as a coloured dot rather than
/// shipped as binary <c>.ico</c> assets — the same approach (and the same reason: no
/// binaries in the repo) as the tray Bridge's <c>TrayIcons</c>.
/// </summary>
internal sealed class LauncherIcons : IDisposable
{
    private readonly Dictionary<RecorderState, Icon> _icons = new()
    {
        [RecorderState.Preflight] = Dot(Color.Goldenrod),
        [RecorderState.Running] = Dot(Color.LimeGreen),
        [RecorderState.Stopped] = Dot(Color.Gray),
        [RecorderState.Failed] = Dot(Color.Firebrick),
        // Running, but not ours: the dashboard works, the tray just will not stop it.
        [RecorderState.Unmanaged] = Dot(Color.LimeGreen),
    };

    public Icon this[RecorderState state] => _icons[state];

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

        // Wrap the 16x16 PNG in a one-entry ICONDIR and hand it to Icon(Stream), which
        // copies the data. Avoids GetHicon(), whose HICON would need a DestroyIcon
        // P/Invoke to release — so there is no unmanaged handle to track and no GDI leak.
        using var png = new MemoryStream();
        bitmap.Save(png, ImageFormat.Png);
        byte[] pngBytes = png.ToArray();

        using var ico = new MemoryStream();
        using (var w = new BinaryWriter(ico, System.Text.Encoding.UTF8, leaveOpen: true))
        {
            w.Write((short)0);              // reserved
            w.Write((short)1);              // type: icon
            w.Write((short)1);              // image count
            w.Write((byte)16);              // width
            w.Write((byte)16);              // height
            w.Write((byte)0);               // palette size (0 = no palette)
            w.Write((byte)0);               // reserved
            w.Write((short)1);              // colour planes
            w.Write((short)32);             // bits per pixel
            w.Write(pngBytes.Length);       // image byte count
            w.Write(22);                    // offset of the image data (6 + 16)
            w.Write(pngBytes);
        }

        ico.Position = 0;
        return new Icon(ico);
    }

    public void Dispose()
    {
        foreach (Icon icon in _icons.Values)
            icon.Dispose();
        _icons.Clear();
    }
}
