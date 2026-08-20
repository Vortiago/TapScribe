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
    public void IsDeclaredFailure_AcceptsEveryTypeTheSeamDocuments(Type declared)
    {
        // One case per <exception> tag on IAudioDeviceEnumerator.Open: the list IS the contract.
        Assert.True(CaptureSeam.IsDeclaredFailure((Exception)Activator.CreateInstance(declared)!));
    }

    [Fact]
    public void IsDeclaredFailure_AcceptsAPlatformFailureThroughItsBaseType()
    {
        // COMException and CoreAudioException are both ExternalException, which is why the seam
        // declares the base type.
        Assert.True(CaptureSeam.IsDeclaredFailure(new COMException("device in use")));
    }

    [Theory]
    [InlineData(typeof(NullReferenceException))]
    [InlineData(typeof(IndexOutOfRangeException))]
    [InlineData(typeof(InvalidCastException))]
    public void IsDeclaredFailure_RejectsAProgrammingMistake(Type bug)
    {
        // The half a catch-all loses: no device produces these, so swallowing one hides a bug.
        Assert.False(CaptureSeam.IsDeclaredFailure((Exception)Activator.CreateInstance(bug)!));
    }
}
