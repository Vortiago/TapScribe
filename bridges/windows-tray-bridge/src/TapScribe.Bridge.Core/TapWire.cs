namespace TapScribe.Bridge.Core;

/// <summary>
/// The fixed `/tap` wire-contract constants every Bridge must match exactly.
/// See bridges/README.md and tapscribe/auth.py. These are the same constants
/// the Recorder and the local-test-bridge use: 16 kHz mono int16, 20 ms frames.
/// </summary>
public static class TapWire
{
    /// <summary>Target sample rate on the wire: 16 kHz.</summary>
    public const int SampleRate = 16_000;

    /// <summary>Target channel count on the wire: mono.</summary>
    public const int Channels = 1;

    /// <summary>Samples per frame: 20 ms @ 16 kHz = 320 samples.</summary>
    public const int FrameSamples = 320;

    /// <summary>
    /// Bytes per frame: 320 samples x 2 bytes (int16) = 640 bytes. One frame
    /// is sent per binary WebSocket message; partial frames are never sent.
    /// </summary>
    public const int FrameBytes = FrameSamples * 2;

    /// <summary>
    /// The bridge prepends this to the tap token and offers the joined string
    /// as a `Sec-WebSocket-Protocol` value (matches auth.py:TAP_SUBPROTOCOL_PREFIX).
    /// Versioned so a second scheme can be added later without breaking bridges.
    /// </summary>
    public const string SubprotocolPrefix = "tapscribe.v1.tap.";
}
