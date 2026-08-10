using System.Security.Cryptography;
using System.Text;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// The Windows <see cref="ITapTokenStore"/>: the tap token is protected with DPAPI
/// (CurrentUser scope) and the base64 blob IS the opaque value kept in the settings file,
/// under the same <c>ProtectedToken</c> key it has always used — so an existing operator's
/// file keeps its exact meaning and no migration is needed. The blob is tied to the current
/// Windows user, so another user on the same box can't read it, and the plaintext is never
/// written to disk.
/// </summary>
public sealed class DpapiTapTokenStore : ITapTokenStore
{
    /// <summary>The base64 DPAPI blob for <paramref name="token"/>, or null for an empty
    /// token — which is how <see cref="BridgeSettingsStore"/> is told to drop the key.</summary>
    public string? Write(string token)
    {
        if (string.IsNullOrEmpty(token))
            return null;
        byte[] cipher = ProtectedData.Protect(
            Encoding.UTF8.GetBytes(token), optionalEntropy: null, DataProtectionScope.CurrentUser);
        return Convert.ToBase64String(cipher);
    }

    /// <summary>The plaintext behind <paramref name="atRest"/>, or "" when this user and
    /// machine can't decrypt it.</summary>
    public string Read(string? atRest)
    {
        if (string.IsNullOrEmpty(atRest))
            return "";
        try
        {
            byte[] plain = ProtectedData.Unprotect(
                Convert.FromBase64String(atRest), optionalEntropy: null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(plain);
        }
        catch (CryptographicException)
        {
            // Blob was written under a different user/machine or is corrupt: treat as "no
            // saved token" so the app still launches. What's lost is the saved token; the
            // operator re-enters it in the dialog.
            return "";
        }
        catch (FormatException)
        {
            // The at-rest value isn't valid base64 (hand-edited file): same handling.
            return "";
        }
    }
}
