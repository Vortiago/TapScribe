using System.Buffers.Binary;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using static TapScribe.Bridge.Core.Tests.Fixtures;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>A <see cref="FactAttribute"/> that skips the test at discovery unless
/// faster-whisper (the real ASR backend) is importable — i.e. the Python recorder
/// stack is present. Uses the standard <c>Skip</c> property, which every runner
/// honours (xunit v2's dynamic-skip token is NOT recognised by <c>dotnet test</c>).
/// So the cross-platform CI job, which has no Python deps, skips this cleanly.</summary>
internal sealed class RequiresPythonAsrAttribute : FactAttribute
{
    public RequiresPythonAsrAttribute()
    {
        if (!FasterWhisperImportable())
            Skip = "faster-whisper not importable — the Python recorder stack isn't present here";
    }

    private static bool FasterWhisperImportable()
    {
        try
        {
            string python = Environment.GetEnvironmentVariable("TAPSCRIBE_PYTHON") ?? "python3";
            var psi = new ProcessStartInfo(python)
            {
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            psi.ArgumentList.Add("-c");
            psi.ArgumentList.Add(
                "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('faster_whisper') else 1)");
            using Process? p = Process.Start(psi);
            return p is not null && p.WaitForExit(15_000) && p.ExitCode == 0;
        }
        catch (SystemException)
        {
            return false;
        }
    }
}

/// <summary>
/// Full multi-person, multi-language meeting E2E for the tray bridge against the
/// REAL Python Recorder — not the in-process <see cref="FakeRecorder"/>. The real
/// production Core clients (<see cref="CaptureOrchestrator"/> + TapClient +
/// <see cref="ControlClient"/> + <see cref="MeetingController"/>) stream TWO
/// speakers' real Norwegian + English speech over /tap into a real detached
/// Session; End fires the real end-of-meeting pipeline (strip →
/// transcribe[faster-whisper <c>base</c>] → summarize[command]); and the merged
/// transcript comes out multilingual with a summary. Proves the tray bridge does a
/// multi-person, multi-language meeting end to end ON LINUX with the Windows-only
/// WASAPI capture swapped for a fixture-fed <see cref="FakeAudioCapture"/> — the
/// same seam a future Linux tray's PulseAudio/PipeWire capture would fill.
///
/// Skipped via <see cref="RequiresPythonAsrAttribute"/> when faster-whisper (the
/// real ASR) isn't importable — i.e. the Python recorder stack isn't present, so
/// the dotnet-core-crossplatform CI job (no Python deps) skips it cleanly; the
/// dedicated full-stack CI job and dev machines run it.
/// </summary>
public class RealRecorderMeetingE2ETests
{
    private static readonly TimeSpan PipelineBudget = TimeSpan.FromSeconds(180);

    [RequiresPythonAsr]
    public async Task MultiPersonMultiLanguageMeeting_RealPipeline_ProducesAMultilingualTranscriptAndSummary()
    {
        string repoRoot = FindRepoRoot();
        string audio = Path.Join(repoRoot, "tests", "fixtures", "audio");
        string norwegianWav = Path.Join(audio, "marlene-nb.wav");
        string englishWav = Path.Join(audio, "armstrong-en.wav");
        Assert.True(
            File.Exists(norwegianWav) && File.Exists(englishWav),
            "da/no/en fixtures absent — see tests/fixtures/audio/README.md");

        await using RealRecorder? maybeRec = await RealRecorder.TryStartAsync(repoRoot, batchModel: "base");
        Assert.True(maybeRec is not null, "the Python recorder failed to start though faster-whisper is importable");
        RealRecorder rec = maybeRec!;

        using var http = new HttpClient();
        using var control = new ControlClient("127.0.0.1", rec.Port, tls: false, token: "", http);

        // Start meeting: a real detached Session, two speakers streaming into it.
        string session = await control.CreateDetachedSessionAsync();
        var nora = new FakeAudioCapture(RecorderFormat); // Norwegian speaker
        var ed = new FakeAudioCapture(RecorderFormat); // English speaker
        // A speech-tuned gate: a long hangover so normal pauses within a turn don't
        // chop it into word-fragments (FastGate's 60 ms is for unit tests, not real
        // utterances), and a sensitive open threshold so quiet onsets still open it.
        var gate = new GateOptions
        {
            OpenThreshold = 0.01,
            Hangover = TimeSpan.FromSeconds(3),
            PreRoll = TimeSpan.Zero,
        };
        await using var orchestrator = CaptureOrchestrator.StartAll(
            [
                new PipelineSpec(nora, Tap(rec.Port, "Nora", session)),
                new PipelineSpec(ed, Tap(rec.Port, "Ed", session)),
            ],
            onConnected: _ => { }, onFailed: (_, _) => { },
            gate: gate, stream: FastStream());

        // Feed each capture its real speech in the recorder wire format (16 kHz mono
        // int16), paced in ~500 ms chunks (≈20× real time) so the stream keeps up
        // without dropping frames — both speakers concurrently, like a real meeting.
        // Clean fixture PCM (no headless degradation) passes the recorder's silero-VAD
        // strip, so the real pipeline transcribes it.
        await Task.WhenAll(FeedAsync(nora, ReadWavPcm(norwegianWav)), FeedAsync(ed, ReadWavPcm(englishWav)));
        await Task.Delay(TimeSpan.FromSeconds(1)); // let the last frames drain to the Recorder

        // End meeting: the real Drain closes both taps, then the real pipeline runs.
        var views = new List<PipelineView>();
        var controller = new MeetingController(
            control, session,
            pollDelay: ct => Task.Delay(TimeSpan.FromMilliseconds(500), ct),
            drainAsync: () => orchestrator.DisposeAsync().AsTask());
        controller.Updated += view => { lock (views) views.Add(view); };

        using var cts = new CancellationTokenSource(PipelineBudget);
        await controller.EndAsync(cts.Token);

        // The pipeline reached Done.
        PipelineView last = views[^1];
        Assert.True(last.Phase == PipelinePhase.Done, $"pipeline did not reach Done: {last.Phase} / {last.FailureReason}");

        // The merged transcript carries BOTH speakers and BOTH languages.
        string sessionDir = Path.Join(rec.BaseDir, "recordings", session);
        string transcriptPath = Path.Join(sessionDir, "session-transcript.json");
        Assert.True(File.Exists(transcriptPath), "no merged transcript was written");
        using JsonDocument doc = JsonDocument.Parse(File.ReadAllText(transcriptPath));
        JsonElement root = doc.RootElement;
        string plain = root.GetProperty("plain_text").GetString() ?? "";

        HashSet<string> speakers = root.GetProperty("segments").EnumerateArray()
            .Select(seg => seg.TryGetProperty("speaker", out JsonElement sp) ? sp.GetString() : null)
            .Where(s => !string.IsNullOrEmpty(s))
            .Select(s => s!)
            .ToHashSet(StringComparer.Ordinal);
        Assert.True(
            speakers.Count >= 2,
            $"expected >=2 speakers, got [{string.Join(", ", speakers)}]; plain={Trunc(plain)}");

        HashSet<string> hyp = Tokens(plain);
        // Norwegian-DISTINCTIVE tokens — NOT the reference's language-agnostic proper
        // nouns (Berlin/Paris/Dietrich survive a wrong-language transcription). These
        // prove the Norwegian speaker came out AS Norwegian, not Englishised.
        string[] norwegianMarkers = ["egentlig", "født", "døde", "skuespillerinne"];
        Assert.True(
            norwegianMarkers.Any(hyp.Contains),
            $"no Norwegian-distinctive word in the transcript — was the Norwegian speaker Englishised? {Trunc(plain)}");
        Assert.True(
            Tokens(File.ReadAllText(Path.Join(audio, "armstrong-en.reference.txt"))).Overlaps(hyp),
            $"English speaker's content missing from the merged transcript: {Trunc(plain)}");

        // The summary was produced + persisted from the multilingual transcript.
        Assert.False(string.IsNullOrWhiteSpace(last.SummaryText), "End produced no summary text");
        Assert.True(File.Exists(Path.Join(sessionDir, "session-summary.json")), "no persisted summary");
    }

    private static TapConnectionOptions Tap(int port, string identity, string session) => new()
    {
        Host = "127.0.0.1",
        Port = port,
        Identity = identity,
        Name = identity,
        Session = session,
        Token = "",
    };

    // ── helpers ──────────────────────────────────────────────────────────────

    private static HashSet<string> Tokens(string text) =>
        text.Split([' ', '\n', '\r', '\t', '.', ',', ':', ';', '!', '?', '"', '\''], StringSplitOptions.RemoveEmptyEntries)
            .Where(raw => raw.Length >= 4)
            .ToHashSet(StringComparer.OrdinalIgnoreCase);

    private static string Trunc(string s) => s.Length <= 200 ? s : s[..200];

    /// <summary>Emit <paramref name="pcm"/> into <paramref name="capture"/> in ~500 ms
    /// chunks at ≈20× real time, so the bridge stream sends frames as it goes rather
    /// than dropping a whole turn handed over in one burst.</summary>
    private static async Task FeedAsync(FakeAudioCapture capture, byte[] pcm)
    {
        int chunk = 25 * TapWire.FrameBytes; // 25 frames ≈ 500 ms
        for (int offset = 0; offset < pcm.Length; offset += chunk)
        {
            capture.Emit(pcm[offset..Math.Min(offset + chunk, pcm.Length)]);
            await Task.Delay(25);
        }
    }

    /// <summary>Read a canonical 16 kHz/mono/int16 WAV's PCM data chunk (the
    /// fixtures are recorder-format), locating the "data" subchunk rather than
    /// assuming a fixed header length.</summary>
    private static byte[] ReadWavPcm(string path)
    {
        byte[] all = File.ReadAllBytes(path);
        for (int i = 12; i + 8 <= all.Length;)
        {
            string id = Encoding.ASCII.GetString(all, i, 4);
            int size = BinaryPrimitives.ReadInt32LittleEndian(all.AsSpan(i + 4, 4));
            if (id == "data")
                return all[(i + 8)..Math.Min(i + 8 + size, all.Length)];
            i += 8 + size + (size & 1);
        }
        throw new InvalidDataException($"no data chunk in {path}");
    }

    private static string FindRepoRoot()
    {
        for (DirectoryInfo? d = new(AppContext.BaseDirectory); d is not null; d = d.Parent)
            if (Directory.Exists(Path.Join(d.FullName, "tapscribe"))
                && Directory.Exists(Path.Join(d.FullName, "bridges")))
                return d.FullName;
        throw new DirectoryNotFoundException("could not locate the TapScribe repo root from the test assembly");
    }
}

/// <summary>
/// Starts the real Python Recorder (<c>python -m tapscribe --no-auth …</c>) in a
/// temp base dir configured for a fast CPU pipeline (faster-whisper batch model +
/// a deterministic <c>command</c> summariser), and exposes its port + base dir.
/// <see cref="TryStartAsync"/> returns null when the recorder won't boot. The
/// faster-whisper skip is decided earlier, at discovery, by
/// <see cref="RequiresPythonAsrAttribute"/>; so by the time this runs the ASR stack
/// is known present, and the caller treats a null as a real FAILURE (a healthy
/// stack that still can't start the recorder), not a skip.
/// </summary>
internal sealed class RealRecorder : IAsyncDisposable
{
    private readonly Process _proc;
    private readonly string _baseDir;

    public int Port { get; }
    public string BaseDir => _baseDir;

    private RealRecorder(Process proc, int port, string baseDir)
    {
        _proc = proc;
        Port = port;
        _baseDir = baseDir;
    }

    public static async Task<RealRecorder?> TryStartAsync(string repoRoot, string batchModel)
    {
        string python = Environment.GetEnvironmentVariable("TAPSCRIBE_PYTHON") ?? "python3";
        int port = FreePort();
        string baseDir = Path.Join(Path.GetTempPath(), "tapscribe-tray-e2e-" + Guid.NewGuid().ToString("N"));
        string cfg = Path.Join(baseDir, "config");
        Directory.CreateDirectory(cfg);
        File.WriteAllText(Path.Join(cfg, "batch-model.txt"), batchModel + "\n");
        // A deterministic command summariser: echo a notes line from the merged
        // transcript on stdin — no multi-GB LLM, so this stays a fast CI step.
        string summaryCmd =
            $"{python} -c \"import sys; t=sys.stdin.read().strip(); print('Meeting notes: ' + (t[:200] if t else 'no speech'))\"";
        File.WriteAllText(
            Path.Join(cfg, "summarizer.json"),
            JsonSerializer.Serialize(new { source = "command", command = summaryCmd }));

        var psi = new ProcessStartInfo(python)
        {
            WorkingDirectory = repoRoot,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
        };
        foreach (string arg in new[]
                 { "-m", "tapscribe", "--host", "127.0.0.1", "--port", port.ToString(), "--no-auth", "--no-auto-live" })
            psi.ArgumentList.Add(arg);
        psi.Environment["TAPSCRIBE_BASE_DIR"] = baseDir;
        psi.Environment["PYTHONPATH"] = repoRoot;
        // The production Norwegian specialist (nb-whisper-large) is too slow on CPU
        // for a test; pin the fast nb-whisper-tiny so the cover stays quick while
        // still routing Norwegian to a Norwegian-tuned model.
        psi.Environment["TAPSCRIBE_SPECIALIST_NO"] = "nb-whisper-tiny";

        Process? proc;
        try
        {
            proc = Process.Start(psi);
        }
        catch (SystemException)
        {
            // python not found / not launchable → null → caller fails.
            TryDelete(baseDir);
            return null;
        }
        if (proc is null)
        {
            TryDelete(baseDir);
            return null;
        }

        if (await IsHealthyAsync(proc, port, TimeSpan.FromSeconds(40)))
            return new RealRecorder(proc, port, baseDir);

        // recorder imports tapscribe but never becomes healthy → null → caller fails.
        TryKill(proc);
        TryDelete(baseDir);
        return null;
    }

    private static async Task<bool> IsHealthyAsync(Process proc, int port, TimeSpan timeout)
    {
        using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(1) };
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < timeout)
        {
            if (proc.HasExited)
                return false;
            try
            {
                using HttpResponseMessage r = await http.GetAsync($"http://127.0.0.1:{port}/health");
                if (r.IsSuccessStatusCode)
                    return true;
            }
            catch (HttpRequestException)
            {
                // connection refused while the recorder boots — keep polling.
            }
            catch (TaskCanceledException)
            {
                // request timed out (1 s) — keep polling until the deadline.
            }
            await Task.Delay(300);
        }
        return false;
    }

    private static int FreePort()
    {
        using var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        int port = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }

    private static void TryKill(Process p)
    {
        // best effort: Kill(tree) throws SystemException variants (already-exited /
        // Win32) or AggregateException (partial tree-kill failure).
        try { if (!p.HasExited) p.Kill(entireProcessTree: true); }
        catch (SystemException) { }
        catch (AggregateException) { }
        p.Dispose();
    }

    private static void TryDelete(string dir)
    {
        try { Directory.Delete(dir, recursive: true); } catch (SystemException) { /* best effort */ }
    }

    public ValueTask DisposeAsync()
    {
        TryKill(_proc);
        TryDelete(_baseDir);
        return ValueTask.CompletedTask;
    }
}
