using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace TapScribe.Bundle.Windows;

/// <summary>
/// Opens a URL or a file with the user's default handler, in a process that is NOT a
/// member of this one's job.
///
/// Both halves of that are load-bearing. A plain <c>ShellExecuteEx</c> spawns the browser
/// as our child, so it joins the tray's KILL_ON_JOB_CLOSE job and Quit takes the
/// operator's browser with it. Handing the target to <c>explorer.exe</c> avoided that (an
/// explorer already runs outside our job), but explorer silently DROPS a URL carrying a
/// query string and opens a folder window instead: verified on Windows 11, where
/// <c>http://host/p</c> reaches the browser and <c>http://host/p?k=v</c> does not. Every
/// minted login link is <c>/login?k=…</c>, so "Open dashboard" opened File Explorer every
/// time, and only the FALLBACK (the plain, signed-out URL) ever reached a browser.
///
/// So: spawn a forwarder that handles the whole URL, and let it leave the job explicitly.
/// <c>rundll32 url.dll,FileProtocolHandler</c> is the documented shell entry point for
/// exactly this and keeps the query string; CREATE_BREAKAWAY_FROM_JOB is what the job's
/// JOB_OBJECT_LIMIT_BREAKAWAY_OK exists to permit (see <see cref="JobObject"/>). The
/// browser is then spawned by a process already outside the job, so it is never enrolled.
/// </summary>
public static class ShellTarget
{
    private const uint CreateBreakawayFromJob = 0x01000000;
    private const uint CreateNoWindow = 0x08000000;

    /// <summary>
    /// The command line handed to CreateProcess. Split out from <see cref="Open"/> because
    /// it is the part worth testing: the rest launches a browser, which no test should do.
    ///
    /// The target is quoted, and a target carrying a double quote is refused rather than
    /// escaped. Nothing TapScribe opens can contain one (its own loopback URL, or a log
    /// path under a folder it created), so a quote means something upstream is wrong and
    /// guessing at the escaping would hide it.
    /// </summary>
    /// <exception cref="ArgumentException">The target is blank or contains a quote.</exception>
    public static string CommandLineFor(string target)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(target);
        if (target.Contains('"', StringComparison.Ordinal))
            throw new ArgumentException($"refusing to open a target containing a quote: {target}", nameof(target));

        return $"rundll32.exe url.dll,FileProtocolHandler \"{target}\"";
    }

    /// <summary>
    /// Open <paramref name="target"/>, outside this process's job. Returns once the
    /// forwarder has been created, which is before the handler has finished opening.
    /// </summary>
    /// <exception cref="ArgumentException">See <see cref="CommandLineFor"/>.</exception>
    /// <exception cref="System.ComponentModel.Win32Exception">CreateProcess refused. The
    /// likeliest cause is a nested job that forbids breakaway, which is worth surfacing:
    /// falling back to an in-job launch would put the operator's browser back inside
    /// KILL_ON_JOB_CLOSE, which is the bug this type exists to prevent.</exception>
    public static void Open(string target)
    {
        // Mutable buffer: CreateProcess may write to lpCommandLine.
        var commandLine = new StringBuilder(CommandLineFor(target));
        var startup = new StartupInfo { cb = Marshal.SizeOf<StartupInfo>() };

        if (!CreateProcessW(
                null, commandLine, IntPtr.Zero, IntPtr.Zero, false,
                CreateBreakawayFromJob | CreateNoWindow,
                IntPtr.Zero, null, ref startup, out ProcessInformation info))
        {
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }

        // The forwarder is not ours to wait on or reap — it exits on its own once the
        // handler is launched, and it is deliberately outside our job.
        using (new SafeFileHandle(info.hProcess, ownsHandle: true))
        using (new SafeFileHandle(info.hThread, ownsHandle: true)) { }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcessW(
        string? lpApplicationName,
        StringBuilder lpCommandLine,
        IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool bInheritHandles,
        uint dwCreationFlags,
        IntPtr lpEnvironment,
        string? lpCurrentDirectory,
        ref StartupInfo lpStartupInfo,
        out ProcessInformation lpProcessInformation);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        public int cb;
        public IntPtr lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }
}
