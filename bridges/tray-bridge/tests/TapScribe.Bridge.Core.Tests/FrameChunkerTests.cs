using TapScribe.Bridge.Core;

namespace TapScribe.Bridge.Core.Tests;

public class FrameChunkerTests
{
    [Fact]
    public void WireConstants_Are640ByteFramesOf320Int16Samples()
    {
        Assert.Equal(320, TapWire.FrameSamples);
        Assert.Equal(640, TapWire.FrameBytes);
        Assert.Equal(TapWire.FrameSamples * 2, TapWire.FrameBytes);
    }

    [Fact]
    public void EmitsExact640ByteFrames()
    {
        var chunker = new FrameChunker();

        List<byte[]> frames = chunker.Push(new byte[1280]);

        Assert.Equal(2, frames.Count);
        Assert.All(frames, f => Assert.Equal(640, f.Length));
        Assert.Equal(0, chunker.PendingBytes);
    }

    [Fact]
    public void RetainsPartialTail_AcrossPushes()
    {
        var chunker = new FrameChunker();

        Assert.Single(chunker.Push(new byte[700]));   // 1 frame, 60 left over
        Assert.Equal(60, chunker.PendingBytes);

        Assert.Single(chunker.Push(new byte[580]));    // 60 + 580 = 640 -> 1 frame
        Assert.Equal(0, chunker.PendingBytes);
    }

    [Fact]
    public void BuffersAcrossManySmallPushes_UntilAFrameIsReady()
    {
        var chunker = new FrameChunker();

        for (int i = 0; i < 5; i++)
            Assert.Empty(chunker.Push(new byte[100])); // 500 bytes buffered, no frame yet
        Assert.Equal(500, chunker.PendingBytes);

        Assert.Single(chunker.Push(new byte[200]));     // 700 -> 1 frame, 60 left
        Assert.Equal(60, chunker.PendingBytes);
    }

    [Fact]
    public void PreservesByteOrderAndContent_AcrossFrameBoundary()
    {
        var chunker = new FrameChunker();
        byte[] input = new byte[1280];
        for (int i = 0; i < input.Length; i++)
            input[i] = (byte)(i % 256);

        List<byte[]> frames = chunker.Push(input);

        Assert.Equal(2, frames.Count);
        Assert.Equal(0, frames[0][0]);
        Assert.Equal(639 % 256, frames[0][639]);
        Assert.Equal(640 % 256, frames[1][0]);
        Assert.Equal(1279 % 256, frames[1][639]);
    }
}
