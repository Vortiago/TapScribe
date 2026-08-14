using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The macOS <see cref="ITapTokenStore"/>: the tap token lives in the login Keychain and
/// the settings file holds nothing about it. That is the other shape the seam allows, and
/// the reason <see cref="ITapTokenStore.Write"/> may answer null: on Windows the DPAPI blob
/// IS the at-rest value, here there is no at-rest value to serialise.
/// </summary>
public sealed class KeychainTapTokenStore : ITapTokenStore
{
    private const string ServiceName = "TapScribe Tray Bridge";
    private const string AccountName = "tap-token";

    private readonly IKeychainItems _items;

    internal KeychainTapTokenStore(IKeychainItems items) => _items = items;

    /// <summary>Null, always: the secret went to the Keychain, so nothing about it belongs
    /// in the settings file.</summary>
    public string? Write(string token)
    {
        _items.Add(ServiceName, AccountName, token);
        return null;
    }

    /// <summary>The plaintext from the Keychain. <paramref name="atRest"/> is ignored: the
    /// macOS settings file has never carried a token, which is what Write's null means.
    /// </summary>
    public string Read(string? atRest)
    {
        _items.Copy(ServiceName, AccountName, out string? secret);
        return secret!;
    }
}
