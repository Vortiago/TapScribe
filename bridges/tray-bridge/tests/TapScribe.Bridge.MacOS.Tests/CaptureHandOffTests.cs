using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.MacOS.Tests;

/// <summary>
/// What the pump does when the handler behind <c>DataAvailable</c> throws (#420).
///
/// The pump runs on a thread this class creates, so an escaping exception ends the tray
/// PROCESS rather than the buffer it was about. Containing it is therefore not defensive
/// style, and the rule that comes with it is easy to get subtly wrong: the read cursor has to
/// advance even when the handler threw, or the producer sees a ring that never drains and
/// drops every buffer after the first failure.
/// </summary>
public class CaptureHandOffTests
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(10);

    private static readonly AudioFormat Stereo = new(48_000, 2, SampleKind.Float32);

    [Fact]
    public void Write_WhenTheHandlerThrows_CountsItAndKeepsDelivering()
    {
        // Both halves in one test on purpose: they are one rule. A pump that contained the
        // throw but stopped advancing would pass a count-only assertion and still deliver
        // silence for the rest of the meeting.
        using var fourthArrived = new ManualResetEventSlim();
        int delivered = 0;
        List<byte> firstBytes = [];
        var handOff = new CaptureHandOff("test-pump", buffer =>
        {
            lock (firstBytes)
                firstBytes.Add(buffer.Span[0]);
            int seen = Interlocked.Increment(ref delivered);
            if (seen <= 3)
                throw new InvalidOperationException($"handler refused buffer {seen}");
            fourthArrived.Set();
        });
        handOff.Start(Stereo);

        for (int i = 0; i < 4; i++)
            handOff.Write([(byte)i, 0, 0, 0]);

        Assert.True(fourthArrived.Wait(Wait), $"the pump stopped after {delivered} buffers");
        Assert.Equal(3, handOff.HandlerFaults);
        // Asserted on CONTENT, not on the callback count: a pump that contained the throw but
        // left the read cursor where it was would re-deliver slot zero four times, which counts
        // identically and is the bug. Distinct leading bytes are what says the slots advanced.
        lock (firstBytes)
            Assert.Equal([0, 1, 2, 3], firstBytes);
        Assert.Equal(0, handOff.DroppedBuffers);
        handOff.Stop();
    }

    [Fact]
    public void Write_WhenTheHandlerNeverThrows_CountsNoFaults()
    {
        // The other direction, so the count above cannot be "always three" and still pass.
        using var arrived = new CountdownEvent(2);
        var handOff = new CaptureHandOff("test-pump", _ => arrived.Signal());
        handOff.Start(Stereo);

        handOff.Write([1, 2, 3, 4]);
        handOff.Write([5, 6, 7, 8]);

        Assert.True(arrived.Wait(Wait), "the buffers never arrived");
        Assert.Equal(0, handOff.HandlerFaults);
        handOff.Stop();
    }
}
