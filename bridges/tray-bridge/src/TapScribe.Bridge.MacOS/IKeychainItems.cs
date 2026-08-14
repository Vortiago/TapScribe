namespace TapScribe.Bridge.MacOS;

/// <summary>
/// The three Keychain calls <see cref="KeychainTapTokenStore"/> makes, as a seam. It exists
/// for one reason: a test cannot make the real Keychain refuse, and refusing (a locked
/// keychain, an operator dismissing the unlock prompt) is exactly the case the store's
/// policy is about. With the raw calls behind this, the policy is tested on any OS through
/// <c>FakeKeychain</c> and only the P/Invoke half needs a Mac.
///
/// Deliberately a mirror of the C API rather than an abstraction over the Keychain: each
/// method is one <c>SecItem*</c> call and answers with its raw <c>OSStatus</c>, so nothing
/// is decided down here. Every judgement about what a status MEANS belongs to the store.
/// </summary>
internal interface IKeychainItems
{
    /// <summary>SecItemCopyMatching for one generic password.</summary>
    int Copy(string service, string account, out string? secret);

    /// <summary>SecItemAdd of one generic password.</summary>
    int Add(string service, string account, string secret);

    /// <summary>SecItemDelete of one generic password.</summary>
    int Delete(string service, string account);
}

/// <summary>The handful of <c>OSStatus</c> values this code names. Apple's full list is in
/// SecBase.h; a status not named here is simply "the Keychain said no".</summary>
internal static class KeychainStatus
{
    /// <summary>errSecSuccess.</summary>
    public const int Success = 0;

    /// <summary>errSecItemNotFound: no such item, which is the ordinary "no token saved yet"
    /// answer rather than a failure.</summary>
    public const int ItemNotFound = -25300;

    /// <summary>errSecDuplicateItem: an item with this service and account already exists.
    /// SecItemAdd refuses rather than replacing, which is why a save deletes first.</summary>
    public const int DuplicateItem = -25299;

    /// <summary>errSecInteractionNotAllowed: the Keychain would have to prompt and may not
    /// (locked keychain, no UI session).</summary>
    public const int InteractionNotAllowed = -25308;

    /// <summary>errSecNotAvailable: no Keychain to talk to at all, which is what the real
    /// implementation answers when this assembly runs somewhere that is not a Mac.</summary>
    public const int NotAvailable = -25291;
}
