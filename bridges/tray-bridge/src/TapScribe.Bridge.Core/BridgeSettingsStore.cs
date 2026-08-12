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
    /// Load the settings. A missing, corrupt, or unreadable file falls back to
    /// environment-seeded defaults rather than throwing, so the app always launches.
    /// </summary>
    public BridgeSettings Load()
    {
        try
        {
            if (File.Exists(FilePath))
            {
                using FileStream stream = File.OpenRead(FilePath);
                BridgeSettings? loaded = JsonSerializer.Deserialize<BridgeSettings>(stream);
                if (loaded is not null)
                {
                    loaded.Token = ReadToken(loaded.ProtectedToken);
                    return loaded;
                }
            }
        }
        catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
        {
            // Corrupt or unreadable settings file: fall back to seeded defaults rather than
            // failing to launch. What's lost is whatever the operator had saved; they
            // re-save from the dialog, which overwrites the bad file.
        }
        return BridgeSettings.SeedFromEnvironment();
    }

    /// <summary>Save the settings, creating the directory if needed. The plaintext token
    /// is handed to the token store and only its opaque answer is serialised.</summary>
    public void Save(BridgeSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        // Unconditional, empty token included: Write("") is how a platform is told to
        // DELETE an out-of-band secret. Guarding this on a non-empty token would leave a
        // Keychain entry alive after the operator blanked the field.
        settings.ProtectedToken = tokens.Write(settings.Token);
        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        using FileStream stream = File.Create(FilePath);
        JsonSerializer.Serialize(stream, settings, new JsonSerializerOptions { WriteIndented = true });
    }

    // The token read is platform IO — a Keychain the operator declined to unlock, a DPAPI
    // blob from another user, a secrets daemon that isn't up. An implementation is asked to
    // degrade to "" itself, but this half doesn't own the platform, so a denial here means
    // "no saved token" rather than a tray that won't launch. What's lost is the operator's
    // saved token; the rest of their settings still load and they re-enter it in the dialog.
    private string ReadToken(string? atRest)
    {
        try
        {
            return tokens.Read(atRest);
        }
        catch (Exception ex) when (IsPlatformSecretFailure(ex))
        {
            return "";
        }
    }

    // Deliberately the widest filter in this codebase: DPAPI raises CryptographicException,
    // a Keychain binding raises whatever it chooses, and the NEXT platform raises something
    // nobody has listed here — a narrow filter would put the tray back to not launching,
    // which is the bug ReadToken exists to prevent. OutOfMemoryException is excluded because
    // then the process is doomed regardless and swallowing it would only hide that.
    private static bool IsPlatformSecretFailure(Exception ex) => ex is not OutOfMemoryException;
}
