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
        // Delete first, whatever the token is. SecItemAdd refuses an existing item with
        // errSecDuplicateItem instead of replacing it, so a re-saved token would otherwise
        // keep the old one; and an empty token means the operator blanked the field, where
        // deleting is the entire job. A missing item makes this a no-op, which is why the
        // status is not worth inspecting.
        _items.Delete(ServiceName, AccountName);
        if (!string.IsNullOrEmpty(token))
            _items.Add(ServiceName, AccountName, token);
        return null;
    }

    /// <summary>The plaintext from the Keychain, or "" when there is none to be had.
    /// <paramref name="atRest"/> is ignored: the macOS settings file has never carried a
    /// token, which is what Write's null means.</summary>
    public string Read(string? atRest)
    {
        // Every status that is not a secret in hand is "no token": not found is first
        // launch, and the refusals (a locked keychain, a dismissed unlock prompt, an
        // authorisation the operator revoked) are all things the tray cannot fix and must
        // not fail to launch over. What is lost is the saved token for this launch; the
        // operator re-enters it in the dialog, and a save overwrites the unread item.
        return _items.Copy(ServiceName, AccountName, out string? secret) == KeychainStatus.Success
            ? secret ?? ""
            : "";
    }
}
