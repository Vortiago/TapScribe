using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The macOS half of the token seam (#419). What <see cref="ITapTokenStore"/> means here is
/// the login Keychain: the secret lives out-of-band and the settings file holds nothing at
/// all, which is the opposite of the Windows DPAPI store keeping its blob IN the file.
/// Driven through a <see cref="FakeKeychain"/> so the policy (what a missing item, a
/// blanked token or a denied read mean) is tested on every lane; the one test that talks to
/// the real Keychain carries <c>[RequiresMacOS]</c>.
/// </summary>
public class KeychainTapTokenStoreTests
{
    [Fact]
    public void Write_HasNoAtRestValue_SoNoTokenIsEverSerialised()
    {
        // The whole point of the macOS store: BridgeSettingsStore serialises whatever Write
        // returns, so returning null is what keeps the tap token out of the settings JSON
        // entirely. Windows answers with a DPAPI blob here; this side must answer with
        // nothing, token or no token.
        var store = new KeychainTapTokenStore(new FakeKeychain());

        Assert.Null(store.Write("tap-token-abc"));
    }

    [Fact]
    public void WriteThenRead_RoundTripsThePlaintext()
    {
        // The settings file has nothing to hand back, so Read is passed null and still owes
        // the caller the token: the Keychain is where the value actually came from.
        const string secret = "round-trip-token-xyz";
        var store = new KeychainTapTokenStore(new FakeKeychain());

        store.Write(secret);

        Assert.Equal(secret, store.Read(null));
    }

    [Fact]
    public void Read_WithNothingSaved_IsNoToken()
    {
        // First launch: there is no item yet, which is an ordinary answer and not a
        // failure. "" is the word the seam uses for it, and it is what the dialog shows as
        // an empty field; a null would travel on into BridgeSettings.Token.
        Assert.Equal("", new KeychainTapTokenStore(new FakeKeychain()).Read(null));
    }

    [Fact]
    public void Read_IgnoresTheAtRestValue_BecauseTheFileNeverCarriesOne()
    {
        // The macOS half of the seam answers from the Keychain and nowhere else. Windows
        // passes its DPAPI blob through this parameter, so a store that honoured it here
        // would hand back whatever a hand-edited settings file said the token was, which is
        // the one thing keeping the secret out of that file was for.
        const string secret = "keychain-token";
        var store = new KeychainTapTokenStore(new FakeKeychain());
        store.Write(secret);

        Assert.Equal(secret, store.Read("stale-blob-from-a-settings-file"));
    }

    [Fact]
    public void Write_ARotatedToken_ReplacesTheStoredOne()
    {
        // The only path a token CHANGE can take, and the reason Update exists at all:
        // SecItemAdd refuses an existing item with errSecDuplicateItem rather than
        // overwriting it, so a save that stopped at the Add would leave the operator on the
        // OLD token forever while the dialog showed the new one. Rotating a leaked token is
        // exactly when that matters most.
        var store = new KeychainTapTokenStore(new FakeKeychain());
        store.Write("first-token");

        store.Write("second-token");

        Assert.Equal("second-token", store.Read(null));
    }

    [Fact]
    public void Write_AnEmptyToken_DropsTheStoredSecret()
    {
        // "" is how the dialog says "forget my token", and BridgeSettingsStore hands it over
        // unconditionally for exactly this. A cleared token that stayed in the Keychain
        // would come back on the next Load, so the operator could never revoke one.
        //
        // Asserted at the seam rather than through Read: an item left behind holding an
        // empty value reads back as "" too, so only asking the Keychain whether the item is
        // still THERE tells the two apart, and a revoked token that is merely blanked is
        // still a Keychain entry the operator was told had gone.
        var keychain = new FakeKeychain();
        var store = new KeychainTapTokenStore(keychain);
        store.Write("tap-token-abc");

        Assert.Null(store.Write(""));

        Assert.Equal(KeychainStatus.ItemNotFound, keychain.Copy(out _));
        Assert.Equal("", store.Read(null));
    }

    [Fact]
    public void Read_AKeychainThatRefuses_DegradesToNoToken()
    {
        // A locked keychain, or an operator dismissing the unlock prompt. The tray has to
        // launch with the rest of its settings and an empty token field, so a refusal reads
        // exactly like nothing saved. BridgeSettingsStore does catch a throwing token store,
        // but a dismissed prompt is ordinary, not exceptional, so it never gets that far.
        var store = new KeychainTapTokenStore(
            new FakeKeychain { Refuses = KeychainStatus.InteractionNotAllowed });

        Assert.Equal("", store.Read(null));
    }

    [Fact]
    public void Write_WhenTheKeychainRefusesToStore_KeepsThePreviousToken()
    {
        // A save that cannot be stored must not cost the operator the token they already
        // had. Clearing the item first would: the delete lands, the store is refused (the
        // keychain locked between the two calls, the operator dismissed the prompt), and a
        // working token is revoked that nobody asked to clear. Saving the Settings dialog
        // without touching the token field is enough to reach it, since BridgeSettingsStore
        // hands the unchanged token back through Write every time.
        var keychain = new FakeKeychain();
        var store = new KeychainTapTokenStore(keychain);
        store.Write("first-token");

        // Refuses the STORE only. A blanket refusal would block the delete too, closing the
        // very window this is about, and would pass against the delete-then-add bug.
        keychain.RefusesStore = KeychainStatus.InteractionNotAllowed;
        store.Write("second-token");
        keychain.RefusesStore = null;

        Assert.Equal("first-token", store.Read(null));
    }

    [Fact]
    public void ServiceAndAccount_StayTheKeychainItemContract()
    {
        // The operator's token is reachable under this (service, account) pair and no
        // other, so changing either orphans every stored token exactly as renaming
        // windows-tray-bridge.json orphans every saved Windows setting: a change here needs
        // a migration, not an edit. They are also the two strings the operator reads in
        // Keychain Access, which is the other reason they are worth choosing rather than
        // deriving.
        Assert.Equal("TapScribe Tray Bridge", KeychainTapTokenStore.ServiceName);
        Assert.Equal("tap-token", KeychainTapTokenStore.AccountName);
    }

    [RequiresMacOS("reach a login Keychain")]
    public void RealKeychain_AddsCopiesUpdatesAndDeletesAGenericPassword()
    {
        // The one test that talks to the login Keychain, and the only way the P/Invoke half
        // is proved at all: the fake above proves the policy but nothing about whether
        // SecItemAdd was handed a dictionary it understands. Driven at the seam rather than
        // through the store, via TestKeychain, which owns the rule that a live test never
        // runs against the production service.
        SecKeychainItems keychain = TestKeychain.Item("round-trip");

        // Deliberately not ASCII. The secret crosses hand-written interop twice, out through
        // CFDataCreate over Encoding.UTF8's bytes and back through CFDataGetBytePtr over a
        // byte COUNT rather than a terminator, and an ASCII token would pass under a wrong
        // encoding constant or an off-by-one length just as happily. The emoji is a surrogate
        // pair in UTF-16 and four bytes in UTF-8, so it also proves the two lengths are not
        // being confused.
        const string secret = "tøken-æøå-\U0001F511-中文";

        try
        {
            Assert.Equal(KeychainStatus.Success, keychain.Add(secret));
            Assert.Equal(KeychainStatus.Success, keychain.Copy(out string? read));
            Assert.Equal(secret, read);

            // The replace leg, and the reason it is here rather than only against the fake:
            // Update is the one call that sends TWO dictionaries, so a mistake in either is
            // invisible to a double that just overwrites a value. Add refusing the duplicate
            // is what sends a save down this path at all.
            Assert.Equal(KeychainStatus.DuplicateItem, keychain.Add("ignored"));
            Assert.Equal(KeychainStatus.Success, keychain.Update("replaced-token"));
            Assert.Equal(KeychainStatus.Success, keychain.Copy(out string? replaced));
            Assert.Equal("replaced-token", replaced);

            Assert.Equal(KeychainStatus.Success, keychain.Delete());
            // The delete is a claim, not just cleanup: this is what a blanked token relies on.
            Assert.Equal(KeychainStatus.ItemNotFound, keychain.Copy(out _));
        }
        finally
        {
            // Leave the login Keychain as we found it even when an assertion above fails,
            // so a red run does not strand an item that makes the next run fail differently.
            keychain.Delete();
        }
    }
}
