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
    /// <summary>
    /// The Keychain item's service, half of the operator-facing contract this store keeps
    /// (the account below is the other half). Deliberately its OWN constant and NOT the
    /// bundle identifier: kSecAttrService is a free label, what actually scopes an item to
    /// this app is its code signature, and this assembly is a plain net10.0 library that
    /// knows nothing of the shell's Info.plist. Spelling the bundle id here would make a
    /// second declaration of it, so editing the manifest would silently orphan every
    /// operator's token. A change to this string needs a migration, not an edit.
    /// </summary>
    public const string ServiceName = "TapScribe Tray Bridge";

    /// <summary>The Keychain item's account. One token per operator, so it names the secret
    /// rather than a user. Same contract as <see cref="ServiceName"/>.</summary>
    public const string AccountName = "tap-token";

    private readonly IKeychainItems _items;

    /// <summary>A store backed by this Mac's login Keychain.</summary>
    public KeychainTapTokenStore()
        : this(new SecKeychainItems(ServiceName, AccountName))
    {
    }

    internal KeychainTapTokenStore(IKeychainItems items) => _items = items;

    /// <summary>Null, always: the secret went to the Keychain, so nothing about it belongs
    /// in the settings file.</summary>
    public string? Write(string token)
    {
        // An empty token is the operator blanking the field, where removing the item IS the
        // job. A missing item makes it a no-op, which is why the status is not inspected.
        if (string.IsNullOrEmpty(token))
        {
            _items.Delete();
            return null;
        }

        // Add, then replace what is already there. Never clear first: SecItemAdd refuses an
        // existing item with errSecDuplicateItem rather than overwriting it, so a re-save
        // has to go through Update either way, and deleting to make room opens a window
        // where the old token is gone and the new one has not landed. A Keychain that
        // refuses the store in that window (it locked between the two calls, the operator
        // dismissed the prompt) would revoke a working token nobody asked to clear, and
        // saving the Settings dialog without touching the token field reaches it, since
        // BridgeSettingsStore hands the unchanged token back through here every time.
        // Update destroys nothing when it fails.
        if (_items.Add(token) == KeychainStatus.DuplicateItem)
            _items.Update(token);

        // A Keychain that refuses the add leaves the operator believing a token was saved,
        // and this seam has nowhere to say otherwise: the return value is the at-rest value,
        // and throwing would fail BridgeSettingsStore.Save outright, losing every other
        // setting over a secret the tray can still be handed by hand. Reporting it needs a
        // channel the dialog can show, which is a later slice's job.
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
        return _items.Copy(out string? secret) == KeychainStatus.Success
            ? secret ?? ""
            : "";
    }
}
