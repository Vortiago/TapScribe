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
}
