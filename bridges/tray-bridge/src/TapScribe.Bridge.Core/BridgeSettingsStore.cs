using System.Text.Json;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Loads/saves <see cref="BridgeSettings"/> as JSON in <paramref name="directory"/>. An
/// instance, not a static: the platform supplies the directory, the filename and the
/// <paramref name="tokens"/> translation, so this half stays portable and testable against
/// a temp directory and a fake token store.
/// </summary>
public sealed class BridgeSettingsStore(ITapTokenStore tokens, string directory, string fileName)
{
    /// <summary>The settings file this store reads and writes.</summary>
    public string FilePath { get; } = Path.Join(directory, fileName);

    /// <summary>
    /// Load the settings. A missing file falls back to environment-seeded defaults, so the
    /// app always launches.
    /// </summary>
    public BridgeSettings Load()
    {
        if (File.Exists(FilePath))
        {
            using FileStream stream = File.OpenRead(FilePath);
            BridgeSettings? loaded = JsonSerializer.Deserialize<BridgeSettings>(stream);
            if (loaded is not null)
                return loaded;
        }
        return BridgeSettings.SeedFromEnvironment();
    }

    /// <summary>Save the settings, creating the directory if needed.</summary>
    public void Save(BridgeSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        using FileStream stream = File.Create(FilePath);
        JsonSerializer.Serialize(stream, settings, new JsonSerializerOptions { WriteIndented = true });
    }
}
