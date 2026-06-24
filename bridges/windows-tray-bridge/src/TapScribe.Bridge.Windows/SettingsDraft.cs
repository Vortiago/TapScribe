using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// The editable state behind the tray Settings dialog, as plain data — seeded from a
/// <see cref="BridgeSettings"/> (<see cref="Seed"/>) and collected back into one
/// (<see cref="ToSettings"/>). Every *decision* the dialog makes lives here so it's
/// unit-tested without WinForms: which <see cref="DeviceSelection"/> to build for each
/// device, how the per-device gate is sourced (slider + shared hangover/pre-roll, with
/// migration-aware seeding and pinned-device defaults), and the carry-forward of a pin
/// whose device is currently absent. <c>SettingsForm</c> is a thin two-way binding of its
/// controls onto these fields — read on build, written back on Save.
/// </summary>
public sealed class SettingsDraft
{
    // Connection.
    public string Host { get; set; } = "";
    public int Port { get; set; }
    public bool Tls { get; set; }
    public string Token { get; set; } = "";

    // The two simple follow-default rows (mic + system loopback).
    public bool MicEnabled { get; set; }
    public string MicName { get; set; } = "";
    public int MicSensitivity { get; set; }
    public bool SystemEnabled { get; set; }
    public string SystemName { get; set; } = "";
    public int SystemSensitivity { get; set; }

    // Gate knobs shared across every device (sensitivity is per-device, above / in the rows).
    public int HangoverMs { get; set; }
    public int PreRollMs { get; set; }

    // The Advanced pin grid, as plain rows the form renders and the user edits in place.
    public IReadOnlyList<PinnedDeviceRow> DeviceRows { get; private set; } = [];

    /// <summary>Whether the saved selection pinned any specific device — the dialog opens
    /// the Advanced pin section when so, so a saved pin isn't hidden behind the expander.</summary>
    public bool HasSavedPins => _savedDevices.OfType<DeviceSelection.Pinned>().Any();

    // Passed through unchanged on Save: the base identity/name (they feed DefaultDevices
    // when nothing is saved), the pins whose device is currently absent (carried verbatim
    // so an unplugged-device pin isn't silently erased), and the normalised saved devices
    // (so a migrated pinned gate is recovered rather than reset — ADR-0007).
    private string _baseIdentity = "";
    private string _baseName = "";
    private IReadOnlyList<DeviceSelection> _savedDevices = [];
    private IReadOnlyList<DeviceSelection> _effectiveDevices = [];
    private IReadOnlyList<DeviceSelection> _absentPinned = [];

    private SettingsDraft() { }

    /// <summary>Seed the editable state from the current settings: connection fields, the
    /// two follow-default rows (ticked + named + sensitivity from the saved/migrated
    /// tuning), and the shared hangover/pre-roll. Call <see cref="SetAvailableDevices"/>
    /// next to populate the pin grid.</summary>
    public static SettingsDraft Seed(BridgeSettings current)
    {
        ArgumentNullException.ThrowIfNull(current);
        var draft = new SettingsDraft
        {
            Host = current.Host,
            Port = current.Port,
            Tls = current.Tls,
            Token = current.Token,
            _baseIdentity = current.Identity,
            _baseName = current.Name,
            _savedDevices = current.Devices,
            _effectiveDevices = current.EffectiveDevices,
        };

        // Sensible fallbacks from the default pair so the fields are never blank even when
        // a box starts unticked, then reflect what's actually saved (ticking the boxes for
        // saved follow-defaults — the EffectiveDevices pass runs last, so a saved/migrated
        // value wins over the default-pair fallback).
        foreach (DeviceSelection selection in current.DefaultDevices())
            draft.ApplySimpleRow(selection, tick: false);
        foreach (DeviceSelection selection in draft._effectiveDevices)
            draft.ApplySimpleRow(selection, tick: true);

        // After migration all devices share hangover/pre-roll, so the first effective
        // device is representative; the flow default covers an empty selection.
        GateSettings shared = draft._effectiveDevices.FirstOrDefault()?.Gate
            ?? GateSettings.DefaultForFlow(DeviceFlow.Capture);
        draft.HangoverMs = shared.HangoverMs;
        draft.PreRollMs = shared.PreRollMs;
        return draft;
    }

    private void ApplySimpleRow(DeviceSelection selection, bool tick)
    {
        if (selection is DeviceSelection.FollowDefault { Flow: DeviceFlow.Capture } mic)
        {
            MicEnabled |= tick;
            MicName = SelectionLabel(mic);
            MicSensitivity = SensitivityOf(mic, DeviceFlow.Capture);
        }
        else if (selection is DeviceSelection.FollowDefault { Flow: DeviceFlow.Render } system)
        {
            SystemEnabled |= tick;
            SystemName = SelectionLabel(system);
            SystemSensitivity = SensitivityOf(system, DeviceFlow.Render);
        }
    }

    /// <summary>
    /// (Re)build the pin grid against the devices present now: one row per device,
    /// pre-ticked + named from the saved pins, carrying its kind for the display label and
    /// the pinned-device gate default. A saved pin whose device is currently absent has no
    /// row and is remembered for verbatim carry-forward on <see cref="ToSettings"/>.
    /// </summary>
    public void SetAvailableDevices(IReadOnlyList<CaptureDevice> available)
    {
        ArgumentNullException.ThrowIfNull(available);

        var savedPinned = new Dictionary<string, DeviceSelection.Pinned>(StringComparer.Ordinal);
        foreach (DeviceSelection.Pinned pinned in _savedDevices.OfType<DeviceSelection.Pinned>())
            savedPinned[pinned.DeviceId] = pinned;

        var rows = new List<PinnedDeviceRow>(available.Count);
        foreach (CaptureDevice device in available)
        {
            savedPinned.TryGetValue(device.Id, out DeviceSelection.Pinned? pinned);
            string flowLabel = device.Flow == DeviceFlow.Capture ? "mic" : "loopback";
            string display = $"{device.Name} [{flowLabel}{(device.IsDefault ? ", default" : "")}]";
            rows.Add(new PinnedDeviceRow(device.Id, device.Flow, display)
            {
                Pinned = pinned is not null,
                Name = pinned is not null ? SelectionLabel(pinned) : device.Name,
            });
        }
        DeviceRows = rows;

        var presentIds = new HashSet<string>(available.Select(d => d.Id), StringComparer.Ordinal);
        _absentPinned = _savedDevices
            .Where(d => d is DeviceSelection.Pinned p && !presentIds.Contains(p.DeviceId))
            .ToList();
    }

    /// <summary>Collect the edited state into a new <see cref="BridgeSettings"/>: the two
    /// follow-default rows (when ticked) + the pinned grid rows + the carried-forward
    /// absent pins, each with its per-device gate. The legacy global gate fields are left
    /// null — tuning is persisted per device on each selection.</summary>
    public BridgeSettings ToSettings()
    {
        int hangoverMs = HangoverMs;
        int preRollMs = PreRollMs;
        GateSettings GateFor(int sensitivity) => new(Math.Clamp(sensitivity, 0, 100), hangoverMs, preRollMs);

        // One Name per device, used as both the identity (the Recorder makes it
        // filename-safe) and the display name.
        var selections = new List<DeviceSelection>();
        if (MicEnabled)
        {
            string mic = MicName.Trim();
            selections.Add(new DeviceSelection.FollowDefault(DeviceFlow.Capture, mic, mic, GateFor(MicSensitivity)));
        }
        if (SystemEnabled)
        {
            string system = SystemName.Trim();
            selections.Add(new DeviceSelection.FollowDefault(DeviceFlow.Render, system, system, GateFor(SystemSensitivity)));
        }

        foreach (PinnedDeviceRow row in DeviceRows)
        {
            if (!row.Pinned)
                continue;
            string name = row.Name.Trim();
            // A pinned device has no sensitivity slider, so keep its previously-saved
            // sensitivity (recovered from the NORMALISED saved devices, so a migrated gate
            // survives Save), else default by the device's kind.
            int sensitivity = SavedPinnedSensitivity(row.DeviceId)
                ?? GateSettings.DefaultForFlow(row.Flow).Sensitivity;
            selections.Add(new DeviceSelection.Pinned(row.DeviceId, name, name, GateFor(sensitivity)));
        }

        // Keep pins whose device is currently absent (no row to collect from) — verbatim,
        // including their own saved gate.
        selections.AddRange(_absentPinned);

        return new BridgeSettings
        {
            Host = Host.Trim(),
            Port = Port,
            Tls = Tls,
            Identity = _baseIdentity,
            Name = _baseName,
            Token = Token.Trim(),
            Devices = selections,
        };
    }

    // The sensitivity a device was last pinned with, or null if it wasn't pinned before.
    // Reads EffectiveDevices (not raw saved Devices) so a pinned device that inherited its
    // gate from a migrated legacy global value keeps that value on Save rather than
    // silently resetting to the flow default (ADR-0007's "no reset on upgrade", for pins).
    private int? SavedPinnedSensitivity(string deviceId) =>
        _effectiveDevices
            .OfType<DeviceSelection.Pinned>()
            .FirstOrDefault(p => string.Equals(p.DeviceId, deviceId, StringComparison.Ordinal))
            ?.Gate?.Sensitivity;

    /// <summary>The single per-device label: the saved Name, or its identity for a
    /// legacy/blank Name.</summary>
    public static string SelectionLabel(DeviceSelection selection) =>
        string.IsNullOrWhiteSpace(selection.Name) ? selection.Identity : selection.Name;

    // The slider value for a selection: its own per-device sensitivity, else the flow
    // default. No clamp — the slider (Min/Max 0–100) and ToSettings's GateFor both bound it.
    private static int SensitivityOf(DeviceSelection selection, DeviceFlow flow) =>
        (selection.Gate ?? GateSettings.DefaultForFlow(flow)).Sensitivity;

    /// <summary>The "NN / 100 (RMS threshold ≈ x.xxx)" readout shown next to a sensitivity
    /// slider — pure formatting over <see cref="GateTuning"/>, so the form just renders it.</summary>
    public static string SensitivityLabel(int sensitivity)
    {
        double threshold = GateTuning.SliderToThreshold(sensitivity);
        return $"{sensitivity} / 100   (RMS threshold ≈ {threshold:0.000})";
    }
}

/// <summary>One row of the Advanced pin grid, as plain data: a present device the operator
/// can pin, carrying its kind (for the display label and the gate default) and the
/// in-grid-editable <see cref="Pinned"/> / <see cref="Name"/>.</summary>
public sealed class PinnedDeviceRow(string deviceId, DeviceFlow flow, string displayLabel)
{
    public string DeviceId { get; } = deviceId;
    public DeviceFlow Flow { get; } = flow;
    public string DisplayLabel { get; } = displayLabel;
    public bool Pinned { get; set; }
    public string Name { get; set; } = "";
}
