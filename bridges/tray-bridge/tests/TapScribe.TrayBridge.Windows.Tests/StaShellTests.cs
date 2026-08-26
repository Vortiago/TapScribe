namespace TapScribe.TrayBridge.Windows.Tests;

/// <summary>
/// Tests for the test harness. Unusual, and earned: <see cref="StaShell"/> has taken the test
/// host down twice — once through the shell's OS dependency it was exercising, once through a
/// recursive overload of its own — and each time the whole assembly aborted with no result, so
/// nobody could tell whether the ten fixes under test were right.
///
/// These touch no WinForms and no tray shell at all, so they are the safest code in the
/// project. Their job is attribution: if they pass and a tray test fails, the harness is
/// sound and the failure is a real assertion about the shell. If they are the ones that
/// break, the harness is the suspect again and nothing else in the run needs reading.
/// </summary>
public class StaShellTests
{
    [Fact]
    public void Get_RunsOnAnStaThread_AndReturnsWhatItProduced()
    {
        using var sta = new StaShell();

        // The value comes back (Get used to be an overload of Run, and a lambda whose body is
        // an expression with a value bound to the Func form — so it called itself until the
        // stack ran out), and it was produced on a thread that can host WinForms at all.
        ApartmentState apartment = sta.Get(() => Thread.CurrentThread.GetApartmentState());
        Assert.Equal(ApartmentState.STA, apartment);
        Assert.Equal(42, sta.Get(() => 42));
    }

    [Fact]
    public void Run_RethrowsWhatTheActionThrew()
    {
        using var sta = new StaShell();

        // A test has to read like a direct call for its assertions to mean anything — and this
        // is also why the harness needs no environment probe of its own: a host that cannot
        // build a WinForms component surfaces THAT exception, from the line that asked for it.
        var boom = new InvalidOperationException("from the STA thread");
        var thrown = Assert.Throws<InvalidOperationException>(() => sta.Run(() => throw boom));
        Assert.Same(boom, thrown);

        // ...and the shell is still usable afterwards, so one failing assertion doesn't
        // poison the rest of a test.
        Assert.Equal(1, sta.Get(() => 1));
    }

    [Fact]
    public void Post_QueuesUntilDrained_ThenRunsInOrderOnTheStaThread()
    {
        using var sta = new StaShell();
        var order = new List<string>();

        sta.Run(() =>
        {
            SynchronizationContext ui = SynchronizationContext.Current!;
            ui.Post(_ => order.Add("first"), null);
            ui.Post(_ => order.Add("second"), null);
        });

        // Nothing ran yet: every tray test depends on the shell's UI work happening when the
        // test says so, not whenever the runtime feels like it.
        Assert.Empty(order);
        Assert.Equal(2, sta.Pending);

        IReadOnlyList<Exception> errors = sta.Drain();

        Assert.Equal(["first", "second"], order);
        Assert.Empty(errors);
        Assert.Equal(0, sta.Pending);
    }
}
