using System.Runtime.InteropServices;
using NAudio.CoreAudioApi;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Windows;

/// <summary>
/// The Windows realisation of <see cref="IAudioDeviceEnumerator"/> over NAudio's
/// <see cref="MMDeviceEnumerator"/>: lists active microphones AND loopback-capable
/// render endpoints, and opens a chosen <see cref="CaptureDevice"/> as the matching
/// WASAPI backend (<see cref="WasapiAudioCapture"/> for a mic,
/// <see cref="WasapiLoopbackAudioCapture"/> for a render endpoint). Lives in the
/// Windows project so the cross-platform core never sees MMDevice / DataFlow.
/// </summary>
public sealed class WasapiDeviceEnumerator : IAudioDeviceEnumerator
{
    private readonly MMDeviceEnumerator _enumerator = new();

    public IReadOnlyList<CaptureDevice> List()
    {
        var devices = new List<CaptureDevice>();
        AddEndpoints(devices, DataFlow.Capture);
        AddEndpoints(devices, DataFlow.Render); // render endpoints are the loopback candidates
        return devices;
    }

    private void AddEndpoints(List<CaptureDevice> into, DataFlow flow)
    {
        string? defaultId = null;
        if (_enumerator.HasDefaultAudioEndpoint(flow, Role.Multimedia))
        {
            using MMDevice def = _enumerator.GetDefaultAudioEndpoint(flow, Role.Multimedia);
            defaultId = def.ID;
        }

        foreach (MMDevice device in _enumerator.EnumerateAudioEndPoints(flow, DeviceState.Active))
        {
            using (device)
            {
                into.Add(new CaptureDevice(
                    device.ID,
                    device.FriendlyName,
                    flow == DataFlow.Capture ? DeviceFlow.Capture : DeviceFlow.Render,
                    IsDefault: device.ID == defaultId));
            }
        }
    }

    public IAudioCapture Open(CaptureDevice device)
    {
        ArgumentNullException.ThrowIfNull(device);

        // Resolve the requested id directly instead of re-walking every endpoint (a
        // "Start meeting" opens two devices, so a List() per Open would enumerate the
        // whole device tree several times on the hot path). The id reaches here from
        // persisted settings / the picker UI, so it is validated before use — keeping
        // the resolve clean under CodeQL's C# suite: GetDevice throws for an unknown id,
        // and we confirm the resolved endpoint is active and of the requested flow, so a
        // stale or wrong-flow id becomes a clear ArgumentException rather than opening
        // the wrong device or failing opaquely later.
        MMDevice mmDevice;
        try
        {
            mmDevice = _enumerator.GetDevice(device.Id);
        }
        catch (COMException ex)
        {
            throw new ArgumentException($"Unknown audio device '{device.Id}'.", nameof(device), ex);
        }

        DeviceFlow flow = mmDevice.DataFlow == DataFlow.Capture ? DeviceFlow.Capture : DeviceFlow.Render;
        if (mmDevice.State != DeviceState.Active || flow != device.Flow)
        {
            mmDevice.Dispose();
            throw new ArgumentException(
                $"Audio device '{device.Id}' is not an active {device.Flow} endpoint.", nameof(device));
        }

        return device.Flow == DeviceFlow.Render
            ? new WasapiLoopbackAudioCapture(mmDevice)
            : new WasapiAudioCapture(mmDevice);
    }

    public void Dispose()
    {
        _enumerator.Dispose();
        GC.SuppressFinalize(this);
    }
}
