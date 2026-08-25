using System.Diagnostics;
using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>A <see cref="FactAttribute"/> for the tap smoke: needs a Mac AND someone who can hear
/// it, so it reports as SKIPPED rather than passing silently where it did nothing. A test that
/// returns early looks identical to one that captured audio, which is the reporting mistake this
/// whole file exists to catch.</summary>
internal sealed class RequiresTapSmokeAttribute : FactAttribute
{
    public RequiresTapSmokeAttribute()
    {
        if (!OperatingSystem.IsMacOS())
            Skip = "not running macOS, so this host cannot open a Core Audio process tap";
        else if (Environment.GetEnvironmentVariable("TAPSCRIBE_TAP_SMOKE") != "1")
            Skip = "TAPSCRIBE_TAP_SMOKE is not 1: this plays audio out of the speakers to tap it";
    }
}

/// <summary>
/// The one claim no fake can make: this Mac's Core Audio really hands the tray the audio it is
/// playing (#420). Everything else about the system-audio path runs against
/// <see cref="FakeCoreAudioHal"/>, and the upstream-contract tests prove the symbols resolve and
/// a tap can be CREATED; none of them proves a byte ever arrives.
///
/// It plays a committed fixture through the speakers with <c>afplay</c> and opens the tap through
/// the same public seam a meeting uses (<see cref="MacOSAudioDeviceEnumerator.Open"/>), so what it
/// exercises is the shipped path: CATapDescription via objc_msgSend, the aggregate device around
/// the default output, and an IOProc on it.
///
/// OPT-IN, and not because it is slow. A tap over a Mac playing NOTHING delivers no callbacks at
/// all rather than silent ones, so on a runner with no audio device, or one where nothing can be
/// played, this is indistinguishable from a broken tap. Rather than let that read as a failure or,
/// worse, be papered over with a skip that also hides a real regression, it runs only where
/// someone can hear the fixture:
///
/// <code>TAPSCRIBE_TAP_SMOKE=1 dotnet test tests/TapScribe.Bridge.MacOS.Tests -c Release</code>
///
/// That command is the automated half of the tray README's dev loop. The microphone half still
/// needs a Mac with a microphone and a human to answer the TCC prompt.
/// </summary>
public class SystemAudioTapSmokeTests
{
    private static readonly TimeSpan Listen = TimeSpan.FromSeconds(3);

    [RequiresTapSmoke]
    public void SystemAudio_WhileTheMacIsPlaying_DeliversAudioThroughTheRealHal()
    {
        if (!OperatingSystem.IsMacOS())
            return; // unreachable: [RequiresTapSmoke] skips first. Here for CA1416 only.

        using var hal = new CoreAudioHal();
        using var enumerator = new MacOSAudioDeviceEnumerator(hal);
        CaptureDevice systemAudio = Assert.IsType<CaptureDevice>(
            CaptureDevice.DefaultFor(enumerator.List(), DeviceFlow.Render));

        using IAudioCapture capture = enumerator.Open(systemAudio);
        long nonZeroBytes = 0;
        long buffers = 0;
        capture.DataAvailable += (_, e) =>
        {
            Interlocked.Increment(ref buffers);
            long live = 0;
            foreach (byte b in e.Data.Span)
                if (b != 0)
                    live++;
            Interlocked.Add(ref nonZeroBytes, live);
        };

        using Process speaker = Play(Fixture("marlene-nb.wav"));
        try
        {
            capture.Start();
            Thread.Sleep(Listen);
            capture.Stop();
        }
        finally
        {
            if (!speaker.HasExited)
                speaker.Kill();
        }

        Assert.True(buffers > 0, "the IOProc never fired: no audio reached the tap at all");
        // Non-silence, not merely traffic: a tap bound to the wrong endpoint still delivers
        // buffers, and they are all zero, which is the shape that records a meeting's far side
        // as silence while the status line says it is streaming.
        Assert.True(nonZeroBytes > 0, $"{buffers} buffers arrived and every byte was zero");
    }

    private static Process Play(string wav)
    {
        Process? afplay = Process.Start(new ProcessStartInfo("/usr/bin/afplay", [wav])
        {
            UseShellExecute = false,
            RedirectStandardError = true,
        });
        Assert.True(afplay is not null, "afplay would not start, so nothing was playing to tap");
        // The first buffers land once CoreAudio has the stream running; starting the capture into
        // a device that is already playing is also what a meeting does.
        Thread.Sleep(TimeSpan.FromMilliseconds(500));
        return afplay!;
    }

    private static string Fixture(string name)
    {
        for (DirectoryInfo? d = new(AppContext.BaseDirectory); d is not null; d = d.Parent)
        {
            string candidate = Path.Join(d.FullName, "tests", "fixtures", "audio", name);
            if (File.Exists(candidate))
                return candidate;
        }

        throw new FileNotFoundException($"{name} is not under any tests/fixtures/audio above the test assembly");
    }
}
