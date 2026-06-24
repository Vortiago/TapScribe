using System.Runtime.ExceptionServices;

namespace TapScribe.TrayBridge.Tests;

/// <summary>
/// Runs a test body on a dedicated STA thread — WinForms control creation and clipboard
/// require STA, which xUnit's default worker threads are not — and re-throws any exception
/// (including an xUnit assertion failure) back on the calling thread so the test reports it.
/// </summary>
internal static class Sta
{
    public static void Run(Action body)
    {
        ExceptionDispatchInfo? failure = null;
        var thread = new Thread(() =>
        {
            try { body(); }
            catch (Exception ex) { failure = ExceptionDispatchInfo.Capture(ex); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.IsBackground = true;
        thread.Start();
        thread.Join();
        failure?.Throw();
    }
}
