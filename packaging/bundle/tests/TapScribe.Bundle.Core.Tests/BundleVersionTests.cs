namespace TapScribe.Bundle.Core.Tests;

/// <summary>
/// The runtime stamp (ADR-0024). Its whole job is to change between releases and be stable
/// within one: unstable and every build re-copies 300 MB, unchanging and an upgrade serves
/// the previous release's wheel — the drift ADR-0015's one-wheel rule exists to prevent, and
/// the one <c>ResolveWheel</c> cannot catch.
/// </summary>
public class BundleVersionTests
{
    [Fact]
    public void TheCommitShaIsNotPartOfTheStamp()
    {
        // Source-link fills the SemVer build-metadata field with the sha, so without this a
        // contributor's Mac re-copies the whole interpreter on every single build.
        Assert.Equal("1.4.0", BundleVersion.Normalise("1.4.0+9cd62f9dfe5f1a2b3c4d5e6f"));
    }

    [Fact]
    public void APlainVersionIsKeptAsItIs()
    {
        Assert.Equal("1.4.0", BundleVersion.Normalise("1.4.0"));
    }

    [Fact]
    public void APrereleaseIsItsOwnRelease()
    {
        // "1.4.0-rc1" must NOT collapse onto "1.4.0": they ship different wheels, so sharing
        // one runtime folder is exactly the stale-wheel bug.
        Assert.NotEqual(BundleVersion.Normalise("1.4.0"), BundleVersion.Normalise("1.4.0-rc1"));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("+onlymetadata")]
    public void AnUnreadableVersionStillNamesOneStableFolder(string? informational)
    {
        // Never empty: the stamp becomes a path segment, and an empty one would make the
        // runtime directory the runtime ROOT — so the copy would land on top of every other
        // version's folder.
        Assert.Equal(BundleVersion.Unknown, BundleVersion.Normalise(informational));
        Assert.NotEmpty(BundleVersion.Normalise(informational));
    }

    [Fact]
    public void TheAssemblysOwnVersionIsAUsableStamp()
    {
        // Whatever this build was stamped with, it has to be a path segment: the reflection
        // half is not interesting, but a version that could not be one would be.
        string current = BundleVersion.Current();

        Assert.NotEmpty(current);
        Assert.Equal(-1, current.IndexOfAny(Path.GetInvalidFileNameChars()));
    }
}
