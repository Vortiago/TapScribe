using System.Runtime.InteropServices;

namespace TapScribe.Bridge.Core.Tests;

/// <summary>
/// The capture seam's declared failure set, as a predicate (#421).
///
/// It exists because naming the set by hand went wrong: a Settings level meter filtered on two
/// of the four and the other two escaped an AppKit click handler, which ends the tray. The
/// answer is not a wider catch, since that also swallows a NullReferenceException and reports a
/// programming mistake to the operator as a dead level bar. The answer is one owner for the set
/// the seam documents, so a caller cannot name a subset of it by accident.
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
        // One case per <exception> tag on IAudioDeviceEnumerator.Open. A tag added there without
        // a case here is the drift this predicate exists to stop, so the list is the contract.
        Assert.True(CaptureSeam.IsDeclaredFailure((Exception)Activator.CreateInstance(declared)!));
    }

    [Fact]
    public void IsDeclaredFailure_AcceptsAPlatformFailureThroughItsBaseType()
    {
        // A backend must not leak a platform-specific type above the seam, but the ones that
        // derive from the declared type are still declared: Windows' COMException and the Mac
        // layer's CoreAudioException are both ExternalException.
        Assert.True(CaptureSeam.IsDeclaredFailure(new COMException("device in use")));
    }

    [Theory]
    [InlineData(typeof(NullReferenceException))]
    [InlineData(typeof(IndexOutOfRangeException))]
    [InlineData(typeof(InvalidCastException))]
    public void IsDeclaredFailure_RejectsAProgrammingMistake(Type bug)
    {
        // The half a catch-all loses. These are not failures a device can produce, so a caller
        // that swallows one turns a crash a developer would see in testing into a quiet
        // "no level" an operator cannot act on.
        Assert.False(CaptureSeam.IsDeclaredFailure((Exception)Activator.CreateInstance(bug)!));
    }
}
