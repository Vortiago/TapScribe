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
    public void Write_AnEmptyToken_DropsTheStoredSecret()
    {
        // "" is how the dialog says "forget my token", and BridgeSettingsStore hands it over
        // unconditionally for exactly this. A cleared token that stayed in the Keychain
        // would come back on the next Load, so the operator could never revoke one.
        var store = new KeychainTapTokenStore(new FakeKeychain());
        store.Write("tap-token-abc");

        store.Write("");

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
}
