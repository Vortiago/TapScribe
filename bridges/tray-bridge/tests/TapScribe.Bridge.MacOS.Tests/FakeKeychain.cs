namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>How a test reaches the REAL Keychain, and the only sanctioned way to do it.
/// Every item it hands out is under a service of its own, never
/// <see cref="KeychainTapTokenStore.ServiceName"/>: a test that ran against the production
/// pair would delete the token off the machine of whoever ran the suite. Spelling that
/// service by hand in each test made the safeguard a comment repeated per file, and one
/// paste away from failing silently.</summary>
internal static class TestKeychain
{
    private const string Service = "TapScribe Tray Bridge (test)";

    /// <summary>A live Keychain seam over an item nothing else will touch. The account is
    /// fresh per call so two tests, or two runs of one test, cannot collide.</summary>
    public static SecKeychainItems Item(string what) =>
        new(Service, $"{what}-{Guid.NewGuid()}");
}

/// <summary>
/// A hand-written <see cref="IKeychainItems"/> standing in for the login Keychain (#419):
/// the one item the seam speaks for, plus settable statuses so a test can drive the refusals
/// a real Keychain will not perform on demand (an operator declining the unlock prompt, a
/// locked keychain). Nothing here talks to the OS, so every policy test in this class runs
/// on the ubuntu lane too.
/// </summary>
internal sealed class FakeKeychain : IKeychainItems
{
    private string? _stored;

    /// <summary>When set, every call answers with this instead of doing the work, which is
    /// how a wholly denied Keychain is driven.</summary>
    public int? Refuses { get; set; }

    /// <summary>When set, only the calls that STORE refuse; reads and deletes still work.
    /// Not a convenience over <see cref="Refuses"/>: a blanket refusal blocks the DELETE
    /// too, which closes the very window a destructive save would fall into, so the test
    /// for "a refused store must not cost the operator their existing token" cannot be
    /// written with one knob. It passes against delete-then-add, which is the bug. A real
    /// Keychain does this whenever it locks between two calls, or when an ACL covers
    /// writing an item but not finding it.</summary>
    public int? RefusesStore { get; set; }

    private int? StoreRefusal => RefusesStore ?? Refuses;

    public int Copy(out string? secret)
    {
        secret = null;
        if (Refuses is int refusal)
            return refusal;
        if (_stored is null)
            return KeychainStatus.ItemNotFound;
        secret = _stored;
        return KeychainStatus.Success;
    }

    // Refuses an EXISTING item rather than replacing it, because that is what SecItemAdd
    // does. A double that upserted here would hide the bug this models: a second save
    // silently keeping the FIRST token.
    public int Add(string secret)
    {
        if (StoreRefusal is int refusal)
            return refusal;
        if (_stored is not null)
            return KeychainStatus.DuplicateItem;
        _stored = secret;
        return KeychainStatus.Success;
    }

    // Refuses a MISSING item, because SecItemUpdate does: it updates what a query matched,
    // and a query that matched nothing is errSecItemNotFound. A double that inserted here
    // would let a caller skip Add entirely and still look correct.
    public int Update(string secret)
    {
        if (StoreRefusal is int refusal)
            return refusal;
        if (_stored is null)
            return KeychainStatus.ItemNotFound;
        _stored = secret;
        return KeychainStatus.Success;
    }

    public int Delete()
    {
        if (Refuses is int refusal)
            return refusal;
        if (_stored is null)
            return KeychainStatus.ItemNotFound;
        _stored = null;
        return KeychainStatus.Success;
    }
}
