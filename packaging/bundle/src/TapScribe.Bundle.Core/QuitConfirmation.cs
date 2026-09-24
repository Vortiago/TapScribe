namespace TapScribe.Bundle.Core;

/// <summary>
/// Whether a Bundle's Quit should ask first, and in what words. Both shells show the same
/// dialog, so the decision and the text live here, where the Linux CI leg tests them, and the
/// shells keep only the native dialog call.
///
/// A Bundle's Quit stops the Recorder this tray started, and the Recorder's strip /
/// transcribe / summarize jobs live only in that process: quitting mid-job ends the work, and
/// nothing resumes it on the next launch. Before the tray owned the Recorder, quitting the
/// Bridge left it running, so this is a cost the one-tray shape introduced (ADR-0022) and the
/// dialog is how it is paid knowingly rather than by surprise.
/// </summary>
public static class QuitConfirmation
{
    public const string Title = "Quit TapScribe?";

    /// <summary>The closing line where the dialog's buttons cannot carry their own words
    /// (a WinForms MessageBox is Yes / No).</summary>
    public const string Question = "Quit anyway?";

    public const string QuitButton = "Quit";

    public const string KeepRunningButton = "Keep running";

    /// <summary>How long Quit waits for the Recorder to say whether it is busy. Short: past it
    /// the answer is "could not tell", which quits.</summary>
    public static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(2);

    /// <summary>
    /// The warning to show before quitting, or null to quit straight away.
    /// </summary>
    /// <param name="stopsRecorder">Whether this Quit stops a Recorder this tray started
    /// (<see cref="HostController.OwnsRunningRecorder"/>). A Recorder somebody else started
    /// outlives the Quit, and so does its work.</param>
    /// <param name="activeJobs">How many jobs that Recorder reported in flight, or null when it
    /// could not be asked. Unknown is not busy: a Recorder that cannot answer is not doing
    /// much, and a Quit must never be held hostage to a probe that failed.</param>
    public static string? WarningFor(bool stopsRecorder, int? activeJobs) =>
        stopsRecorder && activeJobs > 0
            ? "TapScribe is still processing a recording. Quitting stops that work, and it does "
              + "not pick up again on its own: you can run it again from the dashboard later."
            : null;
}
