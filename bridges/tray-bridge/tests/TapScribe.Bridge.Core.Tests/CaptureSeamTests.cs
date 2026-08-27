using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The capture seam's declared failure set, as a predicate (#421). It exists because a caller
/// listing the set by hand listed two of four. A wider catch is not the answer: that reports a
/// programming mistake to the operator as a dead level bar.
/// </summary>
public class CaptureSeamTests
{
    [Theory]
    [InlineData(typeof(ExternalException))]
    [InlineData(typeof(NotSupportedException))]
    [InlineData(typeof(InvalidOperationException))]
    [InlineData(typeof(ArgumentException))]
    public void IsDeclaredOpenFailure_AcceptsEveryTypeTheSeamDocuments(Type declared)
    {
        // One case per <exception> tag on IAudioDeviceEnumerator.Open: the list IS the contract.
        Assert.True(CaptureSeam.IsDeclaredOpenFailure((Exception)Activator.CreateInstance(declared)!));
    }

    [Fact]
    public void IsDeclaredOpenFailure_AcceptsAPlatformFailureThroughItsBaseType()
    {
        // COMException and CoreAudioException are both ExternalException, which is why the seam
        // declares the base type.
        Assert.True(CaptureSeam.IsDeclaredOpenFailure(new COMException("device in use")));
    }

    [Theory]
    [InlineData(typeof(NullReferenceException))]
    [InlineData(typeof(IndexOutOfRangeException))]
    [InlineData(typeof(InvalidCastException))]
    public void IsDeclaredOpenFailure_RejectsAProgrammingMistake(Type bug)
    {
        // The half a catch-all loses: no device produces these, so swallowing one hides a bug.
        Assert.False(CaptureSeam.IsDeclaredOpenFailure((Exception)Activator.CreateInstance(bug)!));
    }

    [Theory]
    [InlineData(typeof(ExternalException))]
    [InlineData(typeof(InvalidOperationException))]
    public void IsDeclaredCaptureFailure_AcceptsEveryTypeStartAndStopDocument(Type declared)
    {
        // One case per <exception> tag on IAudioCapture.Start and .Stop: the list IS the
        // contract, the way the open set's is.
        Assert.True(CaptureSeam.IsDeclaredCaptureFailure((Exception)Activator.CreateInstance(declared)!));
    }

    [Theory]
    [InlineData(typeof(NotSupportedException))]
    [InlineData(typeof(ArgumentException))]
    public void IsDeclaredCaptureFailure_RejectsWhatOnlyOpeningADeviceCanRaise(Type openOnly)
    {
        // Why this is a second predicate and not a reuse of the first: a capture already
        // holding an endpoint cannot raise these, so either one arriving is a backend bug, and
        // a teardown filtering on the OPEN set would swallow it.
        Assert.False(CaptureSeam.IsDeclaredCaptureFailure((Exception)Activator.CreateInstance(openOnly)!));
    }

    [Fact]
    public void IsDeclaredCaptureFailure_RejectsAProgrammingMistake()
    {
        Assert.False(CaptureSeam.IsDeclaredCaptureFailure(new NullReferenceException()));
    }
}
