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
    public string? Write(string token) => TokenProtection.Protect(token);

    public string Read(string? atRest) => TokenProtection.Unprotect(atRest);
}
