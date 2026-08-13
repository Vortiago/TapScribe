using System.Text.Json.Serialization;

namespace TapScribe.Bridge.Core;

/// <summary>
/// A persisted choice of one device to tap, streaming under an operator-editable
/// <paramref name="Identity"/> / <paramref name="Name"/> with its own per-device level-gate
/// <paramref name="Gate"/> (ADR-0007). A selection is either a <see cref="FollowDefault"/>
/// sentinel (bind to whatever the system default for a flow is at Start) or a
/// <see cref="Pinned"/> concrete endpoint id. Resolved against the devices actually
/// present via <see cref="Resolve"/> — see ADR-0005 for why follow-default beats freezing
/// an endpoint id.
///
/// <paramref name="Gate"/> is nullable so a pre-per-device settings file (no <c>gate</c>
/// key) deserialises with no tuning attached; the gate is then filled in at the boundary
/// that knows the right default — <see cref="BridgeSettings"/> on load (migrating an old
/// global value) and <see cref="Resolve"/> as a final flow-keyed fallback — so every
/// resolved pipeline ends up with a concrete gate.
///
/// The <c>kind</c> discriminator makes the list round-trip through the bridge's JSON
/// settings file (System.Text.Json polymorphism); the persisted shape is a contract,
/// so the discriminator strings are stable.
/// </summary>
[JsonPolymorphic(TypeDiscriminatorPropertyName = "kind")]
[JsonDerivedType(typeof(FollowDefault), "followDefault")]
[JsonDerivedType(typeof(Pinned), "pinned")]
public abstract record DeviceSelection(string Identity, string Name, GateSettings? Gate)
{
    /// <summary>Bind to whatever endpoint is the system default for <paramref name="Flow"/>
    /// at resolution time — survives the operator switching their default mic/output.</summary>
    public sealed record FollowDefault(DeviceFlow Flow, string Identity, string Name, GateSettings? Gate = null)
        : DeviceSelection(Identity, Name, Gate);

    /// <summary>Bind to one specific endpoint by its stable id, regardless of which
    /// device is default — "tap this USB interface, always".</summary>
    public sealed record Pinned(string DeviceId, string Identity, string Name, GateSettings? Gate = null)
        : DeviceSelection(Identity, Name, Gate);

    /// <summary>
    /// The identity a selection actually streams under: its own, or
    /// <paramref name="baseIdentity"/> when it is blank. The ONE owner of that substitution,
    /// so <see cref="Resolve"/>'s collision check and
    /// <see cref="ResolveResult.ToTapOptions"/>'s stamping cannot disagree about which
    /// selections are the same speaker.
    /// </summary>
    public static string EffectiveIdentity(string identity, string baseIdentity) =>
        string.IsNullOrWhiteSpace(identity) ? baseIdentity : identity;

    /// <summary>
    /// Resolve saved <paramref name="selections"/> against the devices
    /// <paramref name="available"/> right now. Late-bound on purpose (ADR-0005): a
    /// follow-default selection picks the current default of its flow.
    ///
    /// <paramref name="baseIdentity"/> is the identity a blank Speaker ID streams under (the
    /// caller's <see cref="TapConnectionOptions.Identity"/>). Supply it and the duplicate check
    /// runs on the identity each device will ACTUALLY tap under, so a blank one colliding with
    /// a verbatim base identity is refused here, before a device is opened. A caller that has
    /// no base identity omits it and the raw identities are compared.
    /// </summary>
    public static ResolveResult Resolve(
        IReadOnlyList<DeviceSelection> selections,
        IReadOnlyList<CaptureDevice> available,
        string? baseIdentity = null)
    {
        ArgumentNullException.ThrowIfNull(selections);
        ArgumentNullException.ThrowIfNull(available);

        var resolved = new List<ResolvedDevice>();
        var missing = new List<DeviceSelection>();
        foreach (DeviceSelection selection in selections)
        {
            CaptureDevice? device = selection switch
            {
                // Prefer the flow's default endpoint, falling back to the first of that flow
                // when none is configured — the one shared rule (CaptureDevice.DefaultFor),
                // so a meeting taps exactly the endpoint the Settings meter samples.
                FollowDefault followDefault => CaptureDevice.DefaultFor(available, followDefault.Flow),
                Pinned pinned => available.FirstOrDefault(
                    d => d.Id == pinned.DeviceId),
                _ => null,
            };
            if (device is null)
                missing.Add(selection);
            else
                // Every resolved pipeline gets a concrete gate: the selection's own tuning,
                // or — for a selection that carried none — the sensible default for the
                // RESOLVED device's flow (so a loopback still gets the sensitive default).
                resolved.Add(new ResolvedDevice(
                    device, selection.Identity, selection.Name,
                    selection.Gate ?? GateSettings.DefaultForFlow(device.Flow)));
        }

        Func<ResolvedDevice, string> streamingIdentity = baseIdentity is null
            ? static r => r.Identity
            : r => EffectiveIdentity(r.Identity, baseIdentity);
        bool duplicateIdentity = resolved
            .GroupBy(streamingIdentity, StringComparer.Ordinal)
            .Any(g => g.Count() > 1);
        SelectionVerdict verdict =
            resolved.Count == 0 ? SelectionVerdict.NothingToCapture
            : duplicateIdentity ? SelectionVerdict.DuplicateIdentity
            : SelectionVerdict.Ok;
        return new ResolveResult(resolved, missing, verdict);
    }
}

/// <summary>A selection that resolved to a concrete <see cref="CaptureDevice"/>, carrying
/// the identity/name it will stream under and the per-device <see cref="GateSettings"/>
/// its <see cref="LevelGate"/> is built from (always concrete — defaulted by flow when the
/// selection carried none).</summary>
public sealed record ResolvedDevice(CaptureDevice Device, string Identity, string Name, GateSettings Gate);

/// <summary>The outcome of <see cref="DeviceSelection.Resolve"/>: the selections that
/// bound to a present device, the ones that did not, and a <see cref="SelectionVerdict"/>
/// the tray shell branches on before opening any device.</summary>
public sealed record ResolveResult(
    IReadOnlyList<ResolvedDevice> Resolved,
    IReadOnlyList<DeviceSelection> Missing,
    SelectionVerdict Verdict)
{
    /// <summary>
    /// Build the per-device <see cref="TapConnectionOptions"/> for a meeting: each
    /// resolved device taps under its own <c>Identity</c>/<c>Name</c> while sharing the
    /// connection coordinates from <paramref name="baseOptions"/>
    /// (host/port/tls/allow-self-signed/token) and routing into the one detached
    /// <paramref name="session"/> — so the meeting is
    /// isolated from anything else on the Recorder (per-bridge Sessions, ADR-0005).
    /// </summary>
    public IReadOnlyList<TapConnectionOptions> ToTapOptions(
        string session, TapConnectionOptions baseOptions)
    {
        ArgumentNullException.ThrowIfNull(baseOptions);
        return Resolved
            .Select(r =>
            {
                // A blank Speaker ID falls back to the base identity (which
                // ToConnectionOptions already guarantees non-empty), so a tap is never
                // opened under an empty WAV-slug identity; a blank display name falls
                // back to the identity so the dashboard never shows an empty label.
                string identity = DeviceSelection.EffectiveIdentity(r.Identity, baseOptions.Identity);
                string name = string.IsNullOrEmpty(r.Name) ? identity : r.Name;
                return baseOptions with { Identity = identity, Name = name, Session = session };
            })
            .ToList();
    }
}

/// <summary>Whether a resolved selection is fit to start a meeting on. A non-<see cref="Ok"/>
/// verdict is a hard stop the tray shell surfaces as a clear pre-start error; individual
/// <see cref="ResolveResult.Missing"/> entries under an <see cref="Ok"/> verdict are merely
/// a non-fatal warning (the meeting starts on the devices that did resolve).</summary>
public enum SelectionVerdict
{
    /// <summary>At least one device resolved; the meeting can start.</summary>
    Ok,

    /// <summary>No selection resolved to a present device — nothing to capture.</summary>
    NothingToCapture,

    /// <summary>Two resolved devices share a streaming identity; the Recorder would
    /// cross-attribute them. Each device must stream under a distinct identity.</summary>
    DuplicateIdentity,
}
