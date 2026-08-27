using System.Text.Json;

namespace TapScribe.Bridge.Core;

/// <summary>
/// Loads/saves <see cref="BridgeSettings"/> as JSON in <paramref name="directory"/>. An
/// instance, not a static: the platform supplies the directory, the filename, the
/// <paramref name="tokens"/> translation and the <paramref name="fallbackIdentity"/>, so this
/// half stays portable and testable against a temp directory and a fake token store.
/// </summary>
/// <param name="tokens">How the tap token is kept at rest.</param>
/// <param name="directory">Where the settings file lives.</param>
/// <param name="fileName">What it is called: an on-disk contract with the operator.</param>
/// <param name="fallbackIdentity">The shell's own fallback slug, stamped onto everything this
/// store hands out. Here rather than left to each caller because a deserialised file carries
/// no such field, and this is the one place every settings object the app runs on comes from -
/// so it is the one place the stamp cannot be forgotten.</param>
public sealed class BridgeSettingsStore(
    ITapTokenStore tokens, string directory, string fileName, string fallbackIdentity)
{
    /// <summary>The settings file this store reads and writes.</summary>
    public string FilePath { get; } = Path.Join(directory, fileName);

    // Refused rather than trusted, and refused HERE because Load's stamp is the door every
    // settings object the app runs on comes through - including the one path that never touches
    // SeedFromEnvironment, which is where the same refusal already lives. A blank slug makes
    // BridgeSettings.BaseIdentity blank in spite of its "never blank" contract, and every tap
    // then streams under no identity at all, which the Recorder files as its own speaker.
    private readonly string _fallbackIdentity = NonBlank(fallbackIdentity);

    private static string NonBlank(string fallbackIdentity)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(fallbackIdentity);
        return fallbackIdentity;
    }

    // The token this store last saw at rest: what Load read, or what Save wrote. Save compares
    // against it to tell "the operator cleared the field" from "this platform would not tell us
    // what it was". Kept current by BOTH, since a token entered and then blanked in one session
    // never passes through a Load.
    private string _tokenAtRest = "";

    // The opaque value that went with it, so the skip in Save can put it back. A dialog hands
    // Save a settings object rebuilt from its draft, which carries no ProtectedToken at all, so
    // leaving the property alone writes the file WITHOUT the key and destroys the blob the skip
    // exists to protect.
    private string? _protectedAtRest;

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
                    _tokenAtRest = loaded.Token;
                    _protectedAtRest = loaded.ProtectedToken;
                    // The file has no such key and never will: it is the shell's, not the
                    // operator's. Stamped on the way out so nothing downstream can meet a
                    // settings object carrying another platform's name.
                    loaded.FallbackIdentity = _fallbackIdentity;
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
        return BridgeSettings.SeedFromEnvironment(_fallbackIdentity);
    }

    /// <summary>Save the settings, creating the directory if needed. The plaintext token
    /// is handed to the token store and only its opaque answer is serialised.</summary>
    public void Save(BridgeSettings settings)
    {
        ArgumentNullException.ThrowIfNull(settings);
        // Write("") is how a platform is told to DELETE an out-of-band secret, so an empty
        // token is passed through: blanking the field in the dialog has to reach the Keychain,
        // or an entry outlives the settings that referenced it.
        //
        // Unless it was ALREADY empty when this store loaded, which is not the operator saying
        // anything. A platform that REFUSES a read answers "" as well (a locked Keychain, a
        // prompt the operator dismissed, an ACL that no longer trusts this build's ad-hoc
        // signature), and saving anything at all afterwards would then destroy a working token
        // nobody asked to revoke. Skipping also puts ProtectedToken back as it was loaded, so the
        // Windows blob in the file survives the same way.
        if (!string.IsNullOrEmpty(settings.Token) || !string.IsNullOrEmpty(_tokenAtRest))
        {
            settings.ProtectedToken = tokens.Write(settings.Token);
            // What is at rest now, so the NEXT Save can read this one as the operator's word.
            // Without it a token entered and then blanked in one session never reaches Write("")
            // - nothing loaded it, so both halves of the guard are empty - and the item outlives
            // the revocation, to be read back on the next launch.
            _tokenAtRest = settings.Token;
            _protectedAtRest = settings.ProtectedToken;
        }
        else
        {
            settings.ProtectedToken = _protectedAtRest;
        }

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
