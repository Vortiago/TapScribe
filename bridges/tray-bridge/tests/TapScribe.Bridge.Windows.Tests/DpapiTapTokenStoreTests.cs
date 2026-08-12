using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows.Tests;

/// <summary>
/// The Windows half of the token seam. DPAPI (CurrentUser scope) is what
/// <see cref="ITapTokenStore"/> means here, and the opaque value it hands
/// <see cref="BridgeSettingsStore"/> is the same base64 blob that has always sat under the
/// settings file's <c>ProtectedToken</c> key — so an operator's existing file keeps its
/// exact meaning across the hoist into Core, with no migration. Windows-only: DPAPI does
/// not exist on the other platforms, which is why this assembly is separate from the
/// portable Core suite.
/// </summary>
public class DpapiTapTokenStoreTests
{
    [Fact]
    public void WriteThenRead_RoundTripsTheToken_ThroughAnOpaqueBase64Blob()
    {
        const string secret = "round-trip-token-xyz";
        var store = new DpapiTapTokenStore();

        string? atRest = store.Write(secret);

        Assert.NotNull(atRest);
        Assert.DoesNotContain(secret, atRest);   // protected, never the plaintext
        Convert.FromBase64String(atRest);        // base64 — the frozen on-disk shape
        Assert.Equal(secret, store.Read(atRest));
    }

    [Fact]
    public void Read_AForeignOrCorruptBlob_DegradesToNoToken()
    {
        // A blob written by another user/machine, or a hand-edited file, must not crash
        // the tray — it reads back as "no token" so the operator re-enters it.
        var store = new DpapiTapTokenStore();

        Assert.Equal("", store.Read("@@ not valid base64 or DPAPI @@"));
    }

    [Fact]
    public void Write_AnEmptyToken_HasNoAtRestValue()
    {
        // "" is how the dialog says "forget my token", and DPAPI's answer is that there is
        // nothing to persist — which is what makes BridgeSettingsStore drop the key.
        Assert.Null(new DpapiTapTokenStore().Write(""));
    }
}
