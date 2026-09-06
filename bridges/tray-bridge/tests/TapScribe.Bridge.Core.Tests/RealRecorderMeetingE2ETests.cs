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
    // Once per process, not once per decorated test: the probe spawns a Python interpreter, and
    // every job that BUILDS this project pays it at discovery, including the two that skip.
    private static readonly Lazy<bool> Importable = new(FasterWhisperImportable);

    public RequiresPythonAsrAttribute()
    {
        if (!Importable.Value)
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
        (string audio, string norwegianWav, string englishWav) = SpeechFixtures(repoRoot);
        await using RealRecorder rec = await StartRealRecorderAsync(repoRoot);

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
            new CaptureSet([
                new PipelineSpec(nora, Tap(rec.Port, "Nora", session)),
                new PipelineSpec(ed, Tap(rec.Port, "Ed", session)),
            ]),
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
        await controller.EndAsync(cancellationToken: cts.Token);

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

        HashSet<string> speakers = SpeakersIn(root);
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

    /// <summary>
    /// The same meeting, through <see cref="BridgeRuntime"/> instead of around it.
    ///
    /// The test above wires the Core clients by hand; a shell does not. Both hand every decision
    /// to the runtime, so the class an operator's Start and End go through had met only
    /// <see cref="FakeRecorder"/>. What that left unexercised is its sequencing against a server
    /// that takes real time to answer.
    ///
    /// Platform-neutral: capture is <see cref="FakeAudioCapture"/> at the seam CoreAudio and
    /// WASAPI fill, so this covers both shells.
    /// </summary>
    [RequiresPythonAsr]
    public async Task Runtime_AMeetingAgainstTheRealRecorder_PublishesItsSummaryAndReturnsToIdle()
    {
        string repoRoot = FindRepoRoot();
        (_, string norwegianWav, string englishWav) = SpeechFixtures(repoRoot);
        await using RealRecorder rec = await StartRealRecorderAsync(repoRoot);

        // The gate the test above argues for, as the operator's own setting. Spelled out rather
        // than left to DefaultForFlow, which would fail this test for a reason it is not about.
        var gate = new GateSettings(GateTuning.ThresholdToSlider(0.01), HangoverMs: 3000, PreRollMs: 0);
        using var harness = new RuntimeHarness
        {
            RealMint = true,
            Settings = new BridgeSettings
            {
                Host = "127.0.0.1",
                Port = rec.Port,
                // --no-auth, so the tap token is not the subject here.
                Token = "",
                Identity = "Nora",
                Name = "Nora",
                Devices =
                [
                    new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Nora", "Nora", gate),
                    new DeviceSelection.FollowDefault(DeviceFlow.Render, "Ed", "Ed", gate),
                ],
            },
            Budgets = new RuntimeBudgets
            {
                // Not the harness's 10 ms: a Recorder answering a status request that often is
                // load this meeting competes with.
                PollInterval = TimeSpan.FromMilliseconds(500),
                StartSettleTimeout = TimeSpan.FromSeconds(30),
                QuitTeardownCap = TimeSpan.FromSeconds(30),
            },
        };
        FakeAudioCapture nora = harness.AddDevice("mic", DeviceFlow.Capture);
        FakeAudioCapture ed = harness.AddDevice("out", DeviceFlow.Render);

        var runtime = new BridgeRuntime(
            harness.View, harness.Dispatcher, harness.Dependencies, harness.Settings, harness.Budgets);

        // Start meeting.
        runtime.Start();
        await runtime.StartTask!.WaitAsync(TimeSpan.FromSeconds(60));
        Assert.True(harness.View.CanEnd, "the tray never offered End, so no meeting was published");
        string session = harness.SessionIdInUse!;

        await Task.WhenAll(FeedAsync(nora, ReadWavPcm(norwegianWav)), FeedAsync(ed, ReadWavPcm(englishWav)));
        await Task.Delay(TimeSpan.FromSeconds(1)); // let the last frames drain to the Recorder

        // End meeting: the runtime drains both taps, triggers the real pipeline and polls it.
        runtime.End();
        await runtime.EndTask!.WaitAsync(PipelineBudget);

        // The runtime opened ONE window and rendered the finished pipeline into it.
        FakeMeetingWindow window = Assert.Single(harness.View.Windows);
        PipelineView? shown = window.Last;
        Assert.True(shown is not null, "the meeting window was opened but nothing was rendered into it");
        Assert.True(
            shown!.Phase == PipelinePhase.Done,
            $"pipeline did not reach Done: {shown.Phase} / {shown.FailureReason}");
        Assert.False(string.IsNullOrWhiteSpace(shown.SummaryText), "the window was shown without a summary");

        // Both speakers reached one detached session: the far side is attributed, not mixed
        // into the operator's track.
        string sessionDir = Path.Join(rec.BaseDir, "recordings", session);
        using JsonDocument doc = JsonDocument.Parse(
            File.ReadAllText(Path.Join(sessionDir, "session-transcript.json")));
        HashSet<string> speakers = SpeakersIn(doc.RootElement);
        Assert.True(speakers.Count >= 2, $"expected >=2 speakers, got [{string.Join(", ", speakers)}]");

        // And the tray came back, with the meeting in the history Past-meetings reads.
        Assert.True(harness.View.CanStart, "the tray never offered Start again");
        Assert.False(harness.View.CanEnd, "the tray still offers End after the meeting finished");
        MeetingRecord remembered = Assert.Single(harness.HistoryStore.Load().Meetings);
        Assert.Equal(session, remembered.SessionId);
    }

    [RequiresPythonAsr]
    public async Task Runtime_AnAttachedTapAgainstTheRealRecorder_LandsInTheCurrentSession()
    {
        // ADR-0025 end to end, against the real thing: Connect opens taps that name no session,
        // and the Recorder puts them in the one it has open. Everything else in this file mints
        // a detached session and then looks in it — here the whole claim is that the audio ends
        // up somewhere this bridge never named, so the session id is READ BACK from the
        // Recorder's own `/api/state` rather than known in advance.
        string repoRoot = FindRepoRoot();
        (_, string norwegianWav, string englishWav) = SpeechFixtures(repoRoot);
        await using RealRecorder rec = await StartRealRecorderAsync(repoRoot);

        var gate = new GateSettings(GateTuning.ThresholdToSlider(0.01), HangoverMs: 3000, PreRollMs: 0);
        using var harness = new RuntimeHarness
        {
            RealMint = true, // wired, and the point is that it is never reached
            Settings = new BridgeSettings
            {
                Host = "127.0.0.1",
                Port = rec.Port,
                Token = "", // --no-auth, so the tap token is not the subject here
                Identity = "Nora",
                Name = "Nora",
                Devices =
                [
                    new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Nora", "Nora", gate),
                    new DeviceSelection.FollowDefault(DeviceFlow.Render, "Ed", "Ed", gate),
                ],
            },
            Budgets = new RuntimeBudgets
            {
                PollInterval = TimeSpan.FromMilliseconds(500),
                StartSettleTimeout = TimeSpan.FromSeconds(30),
                QuitTeardownCap = TimeSpan.FromSeconds(30),
            },
        };
        FakeAudioCapture nora = harness.AddDevice("mic", DeviceFlow.Capture);
        FakeAudioCapture ed = harness.AddDevice("out", DeviceFlow.Render);

        var runtime = new BridgeRuntime(
            harness.View, harness.Dispatcher, harness.Dependencies, harness.Settings, harness.Budgets);

        runtime.Connect();
        await runtime.ConnectTask!.WaitAsync(TimeSpan.FromSeconds(60));
        Assert.True(harness.View.CanDisconnect, "the tray never offered Disconnect, so nothing attached");

        await Task.WhenAll(FeedAsync(nora, ReadWavPcm(norwegianWav)), FeedAsync(ed, ReadWavPcm(englishWav)));
        await Task.Delay(TimeSpan.FromSeconds(1)); // let the last frames drain to the Recorder

        runtime.Disconnect();
        await runtime.DisconnectTask!.WaitAsync(TimeSpan.FromSeconds(60));

        // Nothing was minted. The real ControlClient is wired and would have answered; it was
        // never asked, which is what "attached" means.
        Assert.False(harness.MintReached.IsCompleted, "connecting minted a detached session");
        Assert.Null(harness.SessionIdInUse);

        // Where the audio actually went: the session the Recorder calls current, read from the
        // same payload the dashboard badges live from.
        using var http = new HttpClient();
        using JsonDocument state = JsonDocument.Parse(
            await http.GetStringAsync(new Uri($"http://127.0.0.1:{rec.Port}/api/state")));
        string current = state.RootElement.GetProperty("current_session").GetString()!;
        Assert.False(string.IsNullOrWhiteSpace(current));

        string sessionDir = Path.Join(rec.BaseDir, "recordings", current);
        Assert.True(Directory.Exists(sessionDir), $"the Recorder's current session has no folder: {sessionDir}");
        string[] wavs = Directory.GetFiles(sessionDir, "*.wav", SearchOption.TopDirectoryOnly);
        // One per speaker, drained and finalised by Disconnect — the barrier that keeps the
        // last Utterance from being a truncated file.
        Assert.True(
            wavs.Any(w => Path.GetFileName(w).Contains("Nora", StringComparison.OrdinalIgnoreCase)),
            $"no WAV for Nora in the current session: [{string.Join(", ", wavs.Select(Path.GetFileName))}]");
        Assert.True(
            wavs.Any(w => Path.GetFileName(w).Contains("Ed", StringComparison.OrdinalIgnoreCase)),
            $"no WAV for Ed in the current session: [{string.Join(", ", wavs.Select(Path.GetFileName))}]");

        // Disconnect triggers nothing, so the tray is idle and Past meetings is untouched: an
        // attached tap has no session id to key a pipeline, a history entry or a resume on.
        Assert.True(harness.View.CanConnect);
        Assert.False(harness.View.CanDisconnect);
        Assert.Empty(harness.HistoryStore.Load().Meetings);
    }

    [RequiresPythonAsr]
    public async Task Runtime_AnAttachedTapAcrossARotation_FollowsTheRecorderIntoTheNewSession()
    {
        // The half of ADR-0025 that no fake can assert, and the reason "the tray never names a
        // session" is a property rather than a phrasing: because the taps carry no session id,
        // it is the RECORDER that decides where each Utterance lands. Rotate the current
        // session mid-attach — the dashboard's "+ new session", or an operator on another
        // machine — and the next Utterance follows, without the tray being told and without it
        // noticing. A bracketed meeting is the opposite by construction: its taps name a
        // session, and a rotation cannot move them.
        //
        // The NEXT Utterance is the unit that moves, deliberately: the Recorder captures the
        // folder at WS open, so a tap spanning the rotation keeps writing where it started, and
        // a fresh TapStream per Utterance is what turns that into "the next one follows".
        string repoRoot = FindRepoRoot();
        (_, string norwegianWav, string englishWav) = SpeechFixtures(repoRoot);
        await using RealRecorder rec = await StartRealRecorderAsync(repoRoot);

        // A short hangover, unlike the meeting tests above: this one wants each feed to CLOSE
        // its Utterance promptly so the rotation lands between two of them. Fragmenting the
        // speech is free here — the claim is about which folder the WAVs are in, and nothing
        // transcribes them.
        var gate = new GateSettings(GateTuning.ThresholdToSlider(0.01), HangoverMs: 400, PreRollMs: 0);
        using var harness = new RuntimeHarness
        {
            RealMint = true, // wired, and never reached: an attached tap mints nothing
            Settings = new BridgeSettings
            {
                Host = "127.0.0.1",
                Port = rec.Port,
                Token = "",
                Identity = "Nora",
                Name = "Nora",
                Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Nora", "Nora", gate)],
            },
            Budgets = new RuntimeBudgets
            {
                PollInterval = TimeSpan.FromMilliseconds(500),
                StartSettleTimeout = TimeSpan.FromSeconds(30),
                QuitTeardownCap = TimeSpan.FromSeconds(30),
            },
        };
        FakeAudioCapture nora = harness.AddDevice("mic", DeviceFlow.Capture);

        var runtime = new BridgeRuntime(
            harness.View, harness.Dispatcher, harness.Dependencies, harness.Settings, harness.Budgets);

        runtime.Connect();
        await runtime.ConnectTask!.WaitAsync(TimeSpan.FromSeconds(60));
        Assert.True(harness.View.CanDisconnect, "the tray never offered Disconnect, so nothing attached");

        using var http = new HttpClient();
        string before = await CurrentSessionAsync(http, rec.Port);

        // Speech, then silence to close what it opened. The trailing silence is the part that
        // matters: a feed that simply stops leaves the gate open, because the gate counts
        // FRAMES and there are none. Frames, not wall-clock, is also why this is deterministic
        // rather than a sleep sized to hope.
        await FeedAsync(nora, ReadWavPcm(norwegianWav));
        await FeedSilenceAsync(nora, TimeSpan.FromSeconds(2));
        await WaitUntilAsync(
            async () => await OpenTapCountAsync(http, rec.Port) == 0
                && WavsWithAudio(Path.Join(rec.BaseDir, "recordings", before)).Length > 0,
            TimeSpan.FromSeconds(30),
            $"the first utterance to close and land in {before}");

        // Rotate, the way the dashboard's "+ new session" does. The tray is not involved and is
        // not told.
        using HttpResponseMessage rotated = await http.PostAsync(
            new Uri($"http://127.0.0.1:{rec.Port}/api/new-session"), content: null);
        rotated.EnsureSuccessStatusCode();
        using JsonDocument rotation = JsonDocument.Parse(await rotated.Content.ReadAsStringAsync());
        string after = rotation.RootElement.GetProperty("current").GetString()!;
        Assert.NotEqual(before, after);

        // The SAME attached tap, still open, still naming no session.
        await FeedAsync(nora, ReadWavPcm(englishWav));
        await FeedSilenceAsync(nora, TimeSpan.FromSeconds(2));
        await WaitUntilAsync(
            async () => await OpenTapCountAsync(http, rec.Port) == 0
                && WavsWithAudio(Path.Join(rec.BaseDir, "recordings", after)).Length > 0,
            TimeSpan.FromSeconds(30),
            $"the utterance after the rotation to land in {after}");

        runtime.Disconnect();
        await runtime.DisconnectTask!.WaitAsync(TimeSpan.FromSeconds(60));

        // Both folders now hold audio — the two waits above are the claim, and each fails by
        // naming the session that stayed empty.
        //
        // And the tray stayed attached throughout: no mint, no session id, nothing in the
        // history. A rotation is not an event it has, or wants.
        Assert.False(harness.MintReached.IsCompleted, "the rotation made the tray mint a session");
        Assert.Null(harness.SessionIdInUse);
        Assert.Empty(harness.HistoryStore.Load().Meetings);
    }

    [RequiresPythonAsr]
    public async Task Runtime_StartingAMeetingWhileAttached_DrainsTheLiveTapIntoTheSessionItWasRecordedInto()
    {
        // The takeover (ADR-0025 §Start-from-Attached) against the real thing. Against
        // FakeRecorder this is an ordering assertion; here it is the operator's actual stake:
        // the room mic was streaming into the live session when they clicked Start meeting, and
        // the sentence they were half-way through has to survive, IN the session it was spoken
        // into, as a complete WAV — not be cut mid-word by a teardown racing the new meeting's
        // first frames.
        //
        // The long hangover is load-bearing: it keeps the Utterance OPEN at the moment Start
        // runs, so the drain is the only thing that can close it. With a short one the tap
        // would already have closed itself and the test would prove nothing.
        string repoRoot = FindRepoRoot();
        (_, string norwegianWav, _) = SpeechFixtures(repoRoot);
        await using RealRecorder rec = await StartRealRecorderAsync(repoRoot);

        var gate = new GateSettings(GateTuning.ThresholdToSlider(0.01), HangoverMs: 3000, PreRollMs: 0);
        using var harness = new RuntimeHarness
        {
            RealMint = true,
            Settings = new BridgeSettings
            {
                Host = "127.0.0.1",
                Port = rec.Port,
                Token = "",
                Identity = "Nora",
                Name = "Nora",
                Devices = [new DeviceSelection.FollowDefault(DeviceFlow.Capture, "Nora", "Nora", gate)],
            },
            Budgets = new RuntimeBudgets
            {
                PollInterval = TimeSpan.FromMilliseconds(500),
                StartSettleTimeout = TimeSpan.FromSeconds(30),
                QuitTeardownCap = TimeSpan.FromSeconds(30),
            },
        };
        FakeAudioCapture nora = harness.AddDevice("mic", DeviceFlow.Capture);

        var runtime = new BridgeRuntime(
            harness.View, harness.Dispatcher, harness.Dependencies, harness.Settings, harness.Budgets);

        runtime.Connect();
        await runtime.ConnectTask!.WaitAsync(TimeSpan.FromSeconds(60));

        using var http = new HttpClient();
        string live = await CurrentSessionAsync(http, rec.Port);

        // HALF the clip, cut mid-sentence: the operator clicks Start while still talking. The
        // whole fixture would not do — it ends in its own trailing silence, which closes the
        // Utterance on the way out and leaves the takeover nothing to drain.
        byte[] speech = ReadWavPcm(norwegianWav);
        byte[] fed = speech[..(speech.Length / 2 / TapWire.FrameBytes * TapWire.FrameBytes)];
        await FeedAsync(nora, fed);
        await WaitUntilAsync(
            async () => await OpenTapCountAsync(http, rec.Port) > 0,
            TimeSpan.FromSeconds(30),
            "the attached tap to open an utterance");

        string liveDir = Path.Join(rec.BaseDir, "recordings", live);

        runtime.Start();
        await runtime.StartTask!.WaitAsync(TimeSpan.FromSeconds(60));

        // The takeover happened: a bracketed meeting, in a session of its own.
        Assert.True(harness.View.CanEnd, "the tray never offered End, so the takeover published nothing");
        Assert.False(harness.View.CanDisconnect, "the tray still thinks it is attached");
        Assert.NotNull(harness.SessionIdInUse);
        Assert.NotEqual(live, harness.SessionIdInUse);

        // The drain RAN: the Recorder has no tap left open. This is the assertion, and the
        // Recorder's own count is what makes it one — a takeover that skipped the drain leaks
        // the attached tap, which stays connected and keeps writing into the session the
        // meeting just replaced, so one microphone ends up as two speakers across two
        // sessions. Nothing feeds the new meeting yet, so with the drain the count is zero;
        // without it, it never falls below one. Polled because the close is a WebSocket
        // handshake whose far side is a real server.
        await WaitUntilAsync(
            async () => await OpenTapCountAsync(http, rec.Port) == 0,
            TimeSpan.FromSeconds(20),
            "the takeover to close the attached tap rather than leave it streaming into "
                + $"{live}");

        // And what it wrote is the whole utterance, not a fragment: the drain is a barrier, so
        // the audio streamed before the click is on disk in the session it was spoken into.
        // (`wave.writeframes` patches the RIFF header on every write, so this measures what the
        // Recorder RECEIVED — a takeover that cut the tap off mid-flight would leave less.)
        byte[] landed = ReadWavPcm(Assert.Single(WavsWithAudio(liveDir)));
        Assert.True(
            landed.Length >= fed.Length * 0.9,
            $"the live session holds {landed.Length} of the {fed.Length} bytes streamed before "
                + "the takeover — the utterance was cut off rather than drained");

        // Quit rather than End: this test is about the takeover, and End would spend three
        // minutes running a pipeline two other tests in this file already cover.
        await runtime.QuitAsync();
    }

    // Who the merged transcript attributes segments to.
    private static HashSet<string> SpeakersIn(JsonElement transcript) =>
        transcript.GetProperty("segments").EnumerateArray()
            .Select(seg => seg.TryGetProperty("speaker", out JsonElement sp) ? sp.GetString() : null)
            .Where(s => !string.IsNullOrEmpty(s))
            .Select(s => s!)
            .ToHashSet(StringComparer.Ordinal);

    // The two speakers' fixtures, and the check that they are on disk.
    private static (string Dir, string Norwegian, string English) SpeechFixtures(string repoRoot)
    {
        string audio = Path.Join(repoRoot, "tests", "fixtures", "audio");
        string norwegian = Path.Join(audio, "marlene-nb.wav");
        string english = Path.Join(audio, "armstrong-en.wav");
        Assert.True(
            File.Exists(norwegian) && File.Exists(english),
            "da/no/en fixtures absent — see tests/fixtures/audio/README.md");
        return (audio, norwegian, english);
    }

    // A null here is a real FAILURE, not a skip: RequiresPythonAsr already decided the ASR stack
    // is present, so a recorder that will not start is a healthy stack that could not boot one.
    private static async Task<RealRecorder> StartRealRecorderAsync(string repoRoot)
    {
        RealRecorder? rec = await RealRecorder.TryStartAsync(repoRoot, batchModel: "base");
        Assert.True(rec is not null, "the Python recorder failed to start though faster-whisper is importable");
        return rec!;
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

    /// <summary>The session the Recorder is putting un-named taps into right now, read from
    /// the same `/api/state` payload the dashboard badges `● live` from.</summary>
    private static async Task<string> CurrentSessionAsync(HttpClient http, int port)
    {
        using JsonDocument state = JsonDocument.Parse(
            await http.GetStringAsync(new Uri($"http://127.0.0.1:{port}/api/state")).ConfigureAwait(false));
        return state.RootElement.GetProperty("current_session").GetString()!;
    }

    /// <summary>How many taps the Recorder has open — its own count, not the bridge's idea of
    /// one, so "the utterance closed" is observed at the end that writes the file.</summary>
    private static async Task<int> OpenTapCountAsync(HttpClient http, int port)
    {
        using JsonDocument state = JsonDocument.Parse(
            await http.GetStringAsync(new Uri($"http://127.0.0.1:{port}/api/state")).ConfigureAwait(false));
        return state.RootElement.GetProperty("active").GetArrayLength();
    }

    /// <summary>
    /// The WAVs in <paramref name="dir"/> that hold audio — header data chunk non-empty.
    ///
    /// Deliberately NOT a "closed file" test, which it cannot be: the Recorder writes through
    /// <c>wave.writeframes</c>, which re-patches the RIFF header on every write, so an
    /// Utterance still streaming already declares its size. Whether a tap is closed is asked of
    /// the Recorder instead (<see cref="OpenTapCountAsync"/>), which is the only end that
    /// knows. What this rules out is a folder with an EMPTY placeholder in it.
    /// </summary>
    private static string[] WavsWithAudio(string dir)
    {
        if (!Directory.Exists(dir))
            return [];
        return [.. Directory.GetFiles(dir, "*.wav", SearchOption.TopDirectoryOnly).Where(HasAudio)];

        static bool HasAudio(string path)
        {
            try
            {
                return ReadWavPcm(path).Length > 0;
            }
            catch (InvalidDataException)
            {
                // Opened but not yet written to: no data chunk at all. Absent for this
                // purpose, and true again on a later poll.
                return false;
            }
            catch (IOException)
            {
                // Mid-write on a platform that locks it. Same answer, same reason.
                return false;
            }
        }
    }

    /// <summary>Poll <paramref name="condition"/> to a deadline. Every wait in the two attached
    /// tests goes through this rather than a bare delay: what they are waiting for is a real
    /// server finishing real work, and a sleep long enough to be safe on CI is a sleep every
    /// run pays.</summary>
    private static async Task WaitUntilAsync(Func<Task<bool>> condition, TimeSpan timeout, string what)
    {
        var sw = Stopwatch.StartNew();
        while (sw.Elapsed < timeout)
        {
            if (await condition().ConfigureAwait(false))
                return;
            await Task.Delay(100).ConfigureAwait(false);
        }
        Assert.Fail($"timed out after {timeout.TotalSeconds:0}s waiting for {what}");
    }

    /// <summary>Emit digital silence, which is what CLOSES an Utterance: the gate counts silent
    /// FRAMES, not wall-clock, so this is deterministic and runs far faster than the hangover
    /// it satisfies. Without it a feed that simply stops leaves the gate open forever — no
    /// frames, no decision.</summary>
    private static async Task FeedSilenceAsync(FakeAudioCapture capture, TimeSpan duration)
    {
        var chunk = new byte[10 * TapWire.FrameBytes]; // 200 ms
        for (int sent = 0; sent < duration.TotalMilliseconds; sent += 200)
        {
            capture.Emit(chunk);
            await Task.Delay(20);
        }
    }

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

        // Drain both redirected pipes so the chatty recorder (uvicorn access logs on
        // every 500 ms poll + tqdm model-download progress) never blocks on a full
        // OS pipe buffer mid-meeting — the classic RedirectStandardOutput-without-a-
        // reader deadlock. The Python fixture drains to a file for the same reason;
        // here we discard (the assertion messages carry the transcript/WAV diag).
        proc.OutputDataReceived += static (_, _) => { };
        proc.ErrorDataReceived += static (_, _) => { };
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();

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
            catch (SystemException)
            {
                // request timed out (TaskCanceledException) or any other transient
                // framework failure while booting — keep polling to the deadline
                // rather than throwing (which would leak proc + temp dir).
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
        catch (SystemException) { /* already exited / Win32 — nothing left to kill */ }
        catch (AggregateException) { /* a child outlived the kill — best effort in teardown */ }
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
