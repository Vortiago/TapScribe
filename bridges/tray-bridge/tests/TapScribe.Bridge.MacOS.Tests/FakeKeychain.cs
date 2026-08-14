namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// A hand-written <see cref="IKeychainItems"/> standing in for the login Keychain (#419):
/// an in-memory dictionary keyed the way the real one is, plus a settable status so a test
/// can drive the refusals a real Keychain will not perform on demand (an operator declining
/// the unlock prompt, a locked keychain). Nothing here talks to the OS, so every policy
/// test in this class runs on the ubuntu lane too.
/// </summary>
internal sealed class FakeKeychain : IKeychainItems
{
    private readonly Dictionary<(string Service, string Account), string> _items = [];

    /// <summary>When set, every call answers with this instead of doing the work, which is
    /// how a denied Keychain is driven.</summary>
    public int? Refuses { get; set; }

    /// <summary>When set, only the calls that STORE answer with this. A real Keychain can
    /// refuse to write while still answering a read or a delete: it locks between two calls,
    /// or an ACL covers writing the item but not finding it. That asymmetry is the whole
    /// difficulty of replacing a secret, so it needs its own knob.</summary>
    public int? RefusesStore { get; set; }

    private int? StoreRefusal => RefusesStore ?? Refuses;

    public int Copy(string service, string account, out string? secret)
    {
        secret = null;
        if (Refuses is int refusal)
            return refusal;
        if (!_items.TryGetValue((service, account), out string? stored))
            return KeychainStatus.ItemNotFound;
        secret = stored;
        return KeychainStatus.Success;
    }

    // Refuses an existing item rather than replacing it, because that is what SecItemAdd
    // does. A double that upserted here would hide the bug this models: a second Write
    // silently keeping the FIRST token.
    public int Add(string service, string account, string secret)
    {
        if (StoreRefusal is int refusal)
            return refusal;
        if (!_items.TryAdd((service, account), secret))
            return KeychainStatus.DuplicateItem;
        return KeychainStatus.Success;
    }

    // Refuses a MISSING item, because SecItemUpdate does: it updates what a query matched,
    // and a query that matched nothing is errSecItemNotFound. A double that inserted here
    // would let a caller skip Add entirely and still look correct.
    public int Update(string service, string account, string secret)
    {
        if (StoreRefusal is int refusal)
            return refusal;
        if (!_items.ContainsKey((service, account)))
            return KeychainStatus.ItemNotFound;
        _items[(service, account)] = secret;
        return KeychainStatus.Success;
    }

    public int Delete(string service, string account)
    {
        if (Refuses is int refusal)
            return refusal;
        return _items.Remove((service, account))
            ? KeychainStatus.Success
            : KeychainStatus.ItemNotFound;
    }
}
