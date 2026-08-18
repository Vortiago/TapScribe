namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// The description CoreAudio is handed to build the tap's aggregate device (#420). Every
/// claim here is a key or a value type that only fails on a Mac, at the moment an operator
/// starts a meeting: a misspelled key is silently ignored and the aggregate comes up carrying
/// no tap, i.e. a meeting that records the far side as silence. Written as a property list so
/// exactly this is reachable from a lane with no audio hardware.
/// </summary>
public class CoreAudioAggregateDescriptionTests
{
    private const string OutputUid = "BuiltInSpeakerDevice";
    private const string TapUid = "9FE89824-D111-4234-82D0-C04BD7265059";

    private static string Plist() =>
        CoreAudioAggregateDescription.Plist("tapscribe-aggregate-1", OutputUid, TapUid);

    [Fact]
    public void Description_BindsTheAggregateToTheOutputDeviceItTaps()
    {
        // Both halves, because they are different keys for the same device and CoreAudio needs
        // both: "master" names the clock the aggregate runs on, and the sub-device list is what
        // actually puts the endpoint in it. An aggregate with the tap but no sub-device has no
        // device to take its timing from.
        string plist = Plist();

        Assert.Contains($"<key>master</key><string>{OutputUid}</string>", plist, StringComparison.Ordinal);
        Assert.Contains(
            $"<key>subdevices</key><array><dict><key>uid</key><string>{OutputUid}</string></dict></array>",
            plist,
            StringComparison.Ordinal);
    }

    [Fact]
    public void Description_ListsTheTapByItsOwnUid()
    {
        // The one entry that makes this an audio SOURCE rather than an ordinary aggregate of
        // output endpoints. The uid is the tap object's own ('tuid'), not the device's.
        Assert.Contains(
            $"<key>taps</key><array><dict><key>uid</key><string>{TapUid}</string>",
            Plist(),
            StringComparison.Ordinal);
    }

    [Fact]
    public void Description_IsPrivateAndUnstacked_SoNothingElseOnTheMacSeesIt()
    {
        // Private keeps the aggregate out of every other app's device list and out of Sound
        // preferences: the operator picked a microphone, not a TapScribe device, and an
        // aggregate published system-wide is one they can accidentally select as their output.
        // Stacked would make it a multi-output device, which is the opposite shape.
        string plist = Plist();

        Assert.Contains("<key>private</key><integer>1</integer>", plist, StringComparison.Ordinal);
        Assert.Contains("<key>stacked</key><integer>0</integer>", plist, StringComparison.Ordinal);
    }

    [Fact]
    public void Description_SpellsEveryFlagAsANumber_NeverAsAPlistBoolean()
    {
        // The value TYPE is the contract, not just the key. CoreAudio documents each of these
        // flags as a CFNumber, and <true/> parses to a CFBoolean, which it does not read: the
        // flag is then silently ignored and the aggregate is published to the whole Mac with
        // drift compensation off. Nothing on a test lane can catch that except this.
        Assert.DoesNotContain("<true/>", Plist(), StringComparison.Ordinal);
        Assert.DoesNotContain("<false/>", Plist(), StringComparison.Ordinal);
    }

    [Fact]
    public void Description_CompensatesTheTapsDriftAgainstTheAggregatesClock()
    {
        // The tap and the output endpoint are two clocks. Without compensation they slip, and
        // a long meeting's far side drifts out of alignment with the operator's own track,
        // which is exactly what a two-speaker transcript cannot survive.
        Assert.Contains("<key>drift</key><integer>1</integer>", Plist(), StringComparison.Ordinal);
    }

    [Fact]
    public void Description_EscapesADeviceUidThatWouldOtherwiseBreakTheDocument()
    {
        // A UID is a vendor string this code does not choose. One carrying & or < turns a
        // valid description into a parse failure, so the aggregate is never created and system
        // audio silently never records - on that operator's Mac only, which is the worst shape
        // a bug can have.
        string plist = CoreAudioAggregateDescription.Plist("agg", "AMP & <Line-In>", TapUid);

        Assert.Contains("AMP &amp; &lt;Line-In&gt;", plist, StringComparison.Ordinal);
        Assert.DoesNotContain("AMP & <Line-In>", plist, StringComparison.Ordinal);
    }

    [Fact]
    public void Description_NamesTheAggregateSoTheOperatorCanTellWhatMadeIt()
    {
        // It is private, so this is not a picker label: it is what shows up in Audio MIDI
        // Setup's aggregate list and in a CoreAudio log when something goes wrong, which is
        // the only place anyone ever reads it.
        Assert.Contains("TapScribe", Plist(), StringComparison.Ordinal);
        Assert.Contains("<key>uid</key><string>tapscribe-aggregate-1</string>", Plist(), StringComparison.Ordinal);
    }
}
